import asyncio, json, os

async def main():
    os.chdir(r"D:\select_stock\stock_selector_api\backend")
    import sys
    sys.path.insert(0, os.getcwd())
    from main import create_app
    from fastapi.routing import APIRoute
    app = create_app()
    routes = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            routes.append({"path": r.path, "methods": sorted(r.methods or [])})
    out = {"count": len(routes), "routes": routes}
    with open(os.path.join(os.environ["TEMP"], "m1_routes_ascii.json"), "w", encoding="ascii") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

asyncio.run(main())
print("OK_ROUTES_DUMPED")
