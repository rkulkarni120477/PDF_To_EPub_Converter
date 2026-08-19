import asyncio, time, sys
sys.path.insert(0, ".")
from app.main import extract_with_azure_di

async def main():
    with open("../test_sample.pdf", "rb") as f:
        data = f.read()
    start = time.time()
    try:
        pages = await asyncio.wait_for(asyncio.to_thread(extract_with_azure_di, data), timeout=180)
        print("done in", time.time() - start, "s, pages:", len(pages), flush=True)
    except asyncio.TimeoutError:
        print("TIMED OUT after", time.time() - start, "s", flush=True)
    except Exception as e:
        print("ERROR after", time.time() - start, type(e).__name__, e, flush=True)

asyncio.run(main())
