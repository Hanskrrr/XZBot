"""Bounded in-memory FIFO for group decisions, with no duplicated message text.

History and the live transcript are ingested before waiting. Pending fragments
from one member can merge; explicit questions and different members keep their
places. This queues decisions, not guaranteed replies or retries of QQ sends.
"""
import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


class QueueStopped(Exception):
    wait_seconds=0


@dataclass(eq=False)
class Ticket:
    scope: str
    user: str
    reply_id: str
    replaceable: bool
    admitted_at: float
    ready: asyncio.Future
    state: str = 'waiting'
    wait_seconds: float = 0
    previous_reply: str | None = None


@dataclass
class Room:
    active: Ticket | None = None
    waiting: list[Ticket] = field(default_factory=list)
    previous_reply: str | None = None


class GroupQueue:
    def __init__(self, *, max_wait_seconds=60, per_group=16, total=128, receipt_seconds=5):
        self.max_wait_seconds=max_wait_seconds
        self.per_group=per_group
        self.total=total
        self.rooms: dict[str,Room]={}
        self._loop=None
        self.receipt_seconds=receipt_seconds
        self._receipts={}

    def _advance(self, scope):
        room=self.rooms.get(scope)
        if room is None:return
        if room.active is None and room.waiting:
            room.active=room.waiting.pop(0)
            room.active.previous_reply=room.previous_reply
            room.active.ready.set_result(None)
        if room.active is None and not room.waiting:self.rooms.pop(scope,None)

    def _remove(self,ticket):
        room=self.rooms.get(ticket.scope)
        if room:
            if room.active is ticket:
                room.active=None
                room.previous_reply=ticket.reply_id
            elif ticket in room.waiting:room.waiting.remove(ticket)
            self._advance(ticket.scope)

    def clear(self,scope=None):
        # The active generator uses the existing privacy-generation guard.
        # Never unlock it early and allow an overlapping cloud call.
        try:current=asyncio.get_running_loop()
        except RuntimeError:current=None
        if self._loop is not None and current is not self._loop:
            if not self._loop.is_closed():self._loop.call_soon_threadsafe(self.clear,scope)
            return
        for key,room in list(self.rooms.items()):
            if scope is not None and key!=scope:continue
            for ticket in room.waiting:
                if not ticket.ready.done():ticket.ready.set_result('queue_context_changed')
            room.waiting.clear()

    def expect_receipt(self,reply_id):
        loop=asyncio.get_running_loop()
        self._receipts={key:value for key,value in self._receipts.items()
                        if value[0].get_loop() is loop and not value[0].done()}
        if len(self._receipts)>=self.total:return
        future=loop.create_future()
        timer=loop.call_later(self.receipt_seconds,self.settle,reply_id)
        self._receipts[reply_id]=(future,timer)

    def settle(self,reply_id):
        item=self._receipts.get(reply_id)
        if not item:return
        loop=item[0].get_loop()
        try:current=asyncio.get_running_loop()
        except RuntimeError:current=None
        if current is not loop:
            if not loop.is_closed():loop.call_soon_threadsafe(self.settle,reply_id)
            return
        future,timer=self._receipts.pop(reply_id)
        timer.cancel()
        if not future.done():future.set_result(None)

    @asynccontextmanager
    async def turn(self,scope,user,reply_id,*,replaceable,lock):
        loop=asyncio.get_running_loop()
        self._loop=loop
        room=self.rooms.get(scope)
        if room:
            for old in list(room.waiting):
                if old.user==user and old.replaceable:
                    room.waiting.remove(old)
                    old.ready.set_result('queue_merged')
        size=sum(len(r.waiting)+(r.active is not None) for r in self.rooms.values())
        waiting_count=len(room.waiting)+int(bool(room.active and room.active.state=='waiting')) if room else 0
        if size>=self.total or waiting_count>=self.per_group:
            raise QueueStopped('queue_full')
        room=self.rooms.setdefault(scope,Room())
        ticket=Ticket(scope,user,reply_id,replaceable,loop.time(),loop.create_future())
        room.waiting.append(ticket)
        self._advance(scope)
        acquired=False
        try:
            try:
                async with asyncio.timeout(self.max_wait_seconds):
                    reason=await asyncio.shield(ticket.ready)
                    if reason:raise QueueStopped(reason)
                    receipt=self._receipts.get(ticket.previous_reply)
                    if receipt and receipt[0].get_loop() is loop:
                        await asyncio.shield(receipt[0])
                    # Idle and peer-bot decisions also use this scope's lock.
                    await lock.acquire()
                    acquired=True
            except TimeoutError:
                raise QueueStopped('queue_expired') from None
            ticket.wait_seconds=max(0,loop.time()-ticket.admitted_at)
            ticket.state='processing'
            yield ticket
        except QueueStopped as exc:
            exc.wait_seconds=max(0,loop.time()-ticket.admitted_at)
            raise
        finally:
            if acquired:lock.release()
            self._remove(ticket)

    def snapshot(self,scope='',user=''):
        now=time.monotonic()
        rows=[]
        for key,room in list(self.rooms.items()):
            if scope and key!=scope:continue
            active=room.active if room.active and (not user or room.active.user==user) else None
            waiting=[t for t in room.waiting if not user or t.user==user]
            if not active and not waiting:continue
            rows.append({'scope':key,'processing':bool(active and active.state=='processing'),
                         'waiting':len(waiting)+int(bool(active and active.state=='waiting')),
                         'oldest_wait_seconds':round(max([now-t.admitted_at for t in waiting+
                            ([active] if active and active.state=='waiting' else [])]+[0]),1)})
        return {'groups':rows,'max_wait_seconds':self.max_wait_seconds,'per_group_limit':self.per_group,
                'total_limit':self.total,'persistent':False}
