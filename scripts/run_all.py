import asyncio
import os
import sys

UVICORN_APP = os.environ.get("UVICORN_APP", "backend.main:app")
UVICORN_HOST = os.environ.get("UVICORN_HOST", "0.0.0.0")
UVICORN_PORT = os.environ.get("UVICORN_PORT", "8000")
UVICORN_LOG_LEVEL = os.environ.get("UVICORN_LOG_LEVEL", "warning")
UVICORN_ACCESS_LOG = os.environ.get("UVICORN_ACCESS_LOG", "false").lower() in ("1", "true", "yes")


async def launch_process(cmd):
    return await asyncio.create_subprocess_exec(*cmd)


async def main():
    python = sys.executable

    uvicorn_cmd = [
        python,
        "-m",
        "uvicorn",
        UVICORN_APP,
        "--host",
        UVICORN_HOST,
        "--port",
        UVICORN_PORT,
        "--log-level",
        UVICORN_LOG_LEVEL,
    ]
    if not UVICORN_ACCESS_LOG:
        uvicorn_cmd.append("--no-access-log")

    compositor_cmd = [python, "scripts/virtual_cam_compositor.py"]

    print(f"Starting backend: {' '.join(uvicorn_cmd)}")
    uvicorn_proc = await launch_process(uvicorn_cmd)

    # Small delay to let backend boot
    await asyncio.sleep(1)

    print(f"Starting virtual cam compositor: {' '.join(compositor_cmd)}")
    compositor_proc = await launch_process(compositor_cmd)

    try:
        await asyncio.gather(uvicorn_proc.wait(), compositor_proc.wait())
    except asyncio.CancelledError:
        pass
    finally:
        for proc in (uvicorn_proc, compositor_proc):
            if proc.returncode is None:
                proc.terminate()
        for proc in (uvicorn_proc, compositor_proc):
            try:
                await proc.wait()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
