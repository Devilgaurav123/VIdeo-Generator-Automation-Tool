"""Container-safe worker slot.

The workspace runner is asyncio-based. This process keeps the compose worker
service healthy until a Redis/Celery deployment enables the task transport.
"""

import asyncio


async def main() -> None:
    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())