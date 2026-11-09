from fastapi import APIRouter, HTTPException, Request


def create_router(service):
    router=APIRouter(prefix='/admin/updates')
    @router.get('')
    def status():return service.status()

    @router.put('')
    async def save(request:Request):
        async with service.lock:
            try:return service.save(await request.json())
            except (ValueError,TypeError):raise HTTPException(400,'update_settings_invalid') from None

    @router.post('/{action}')
    async def operate(action:str,request:Request):
        if action not in {'check','prepare','install'}:raise HTTPException(404,'update_action_unknown')
        try:body=await request.json()
        except ValueError:raise HTTPException(400,'update_fields_invalid') from None
        if body!={}:raise HTTPException(400,'update_fields_invalid')
        if service.lock.locked():raise HTTPException(409,'update_busy')
        async with service.lock:
            try:
                if action=='check':return await service.check()
                if action=='prepare':return await service.prepare()
                return service.install()
            except ValueError as error:
                code=str(error);raise HTTPException(409,code if code.startswith('update_') else 'update_invalid') from None
            except Exception:raise HTTPException(503,'update_operation_failed') from None
    return router
