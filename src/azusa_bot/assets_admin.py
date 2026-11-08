"""Authenticated admin-only expression asset operations for the pinned profile."""
from fastapi import APIRouter, HTTPException, Request, Response

from .assets import AssetStore, AssetError


def create_router(engine):
    router = APIRouter(prefix="/admin/assets")

    def operation(callback):
        try:
            return callback(AssetStore(engine.current.content_root))
        except AssetError as exc:
            raise HTTPException(404 if str(exc)=="asset_not_found" else 400, str(exc)) from None

    async def payload(request):
        try:
            return await request.json()
        except ValueError:
            raise HTTPException(400, "asset_fields_invalid") from None

    @router.get("")
    def listing():
        return dict(operation(lambda store:store.list()), profile_id=engine.current.profile_id)

    @router.post("")
    async def upload(request: Request):
        body = await payload(request)
        asset = operation(lambda store:store.upload(body))
        engine.current.invalidate_groups()
        return {"asset":asset, "profile_id":engine.current.profile_id}

    @router.patch("/{number}")
    async def update(number: str, request: Request):
        body = await payload(request)
        asset = operation(lambda store:store.update(number,body))
        engine.current.invalidate_groups()
        return {"asset":asset, "profile_id":engine.current.profile_id}

    @router.delete("/{number}")
    def delete(number: str):
        result = operation(lambda store:store.delete(number))
        engine.current.invalidate_groups()
        return dict(result, profile_id=engine.current.profile_id)

    @router.get("/{number}/preview")
    def preview(number: str):
        raw, mimetype = operation(lambda store:store.preview(number))
        return Response(raw,media_type=mimetype,headers={"Cache-Control":"no-store", "X-Content-Type-Options":"nosniff"})

    return router
