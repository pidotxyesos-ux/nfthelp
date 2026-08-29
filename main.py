```python
import asyncio
import os
import random
import sqlite3
import sys
import time
from typing import Any

from telethon import TelegramClient, utils
from telethon.errors import (
    FloodWaitError,
    RPCError,
    SessionPasswordNeededError,
    AuthKeyDuplicatedError,
)
from telethon.network.connection.connection import ConnectionTcpFull
from telethon.tl.functions.payments import (
    GetStarGiftsRequest,
    GetResaleStarGiftsRequest,
    SendStarGiftOfferRequest,
)
from telethon.tl.types import StarsAmount


# ============================================================
# CONFIG
# ============================================================

API_ID = 35175774
API_HASH = "919166219f6c1336e4136ed120d01306"

OFFER_STARS = 125

SESSION_NAME = "buyer"
DATABASE_NAME = "buyer.sqlite3"

# Telegram permits only these durations for real offers.
# 21600 = 6 hours
# 43200 = 12 hours
# 86400 = 24 hours
# 129600 = 36 hours
# 172800 = 48 hours
# 259200 = 72 hours
OFFER_DURATION = 21600

# Polling interval.
# Lower values mean more API requests.
POLL_INTERVAL = 3

# Maximum number of resale gifts fetched per page.
PAGE_LIMIT = 100

# Delay between different base gift types.
# This helps avoid hammering Telegram if there are many types.
REQUEST_DELAY = 0.15

# Retry delay after temporary network errors.
NETWORK_RETRY_DELAY = 5

# Maximum number of attempts for a temporary request error.
REQUEST_RETRIES = 5


# ============================================================
# LOGGING
# ============================================================

def log(level: str, message: str) -> None:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} [{level}] {message}", flush=True)


def info(message: str) -> None:
    log("INFO", message)


def nft_log(message: str) -> None:
    log("NFT", message)


def offer_log(message: str) -> None:
    log("OFFER", message)


def success(message: str) -> None:
    log("SUCCESS", message)


def error(message: str) -> None:
    log("ERROR", message)


def wait_log(message: str) -> None:
    log("WAIT", message)


# ============================================================
# SQLITE
# ============================================================

class Database:
    def __init__(self, filename: str):
        self.conn = sqlite3.connect(
            filename,
            check_same_thread=False,
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gifts (
                slug TEXT PRIMARY KEY,
                gift_id INTEGER,
                owner_id INTEGER,
                offer_min_stars INTEGER,
                random_id INTEGER,
                status TEXT NOT NULL,
                first_seen INTEGER NOT NULL,
                last_update INTEGER NOT NULL,
                error TEXT
            )
            """
        )

        self.conn.commit()

    def exists(self, slug: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM gifts WHERE slug = ? LIMIT 1",
            (slug,),
        ).fetchone()

        return row is not None

    def get(self, slug: str):
        return self.conn.execute(
            """
            SELECT
                slug,
                gift_id,
                owner_id,
                offer_min_stars,
                random_id,
                status,
                first_seen,
                last_update,
                error
            FROM gifts
            WHERE slug = ?
            """,
            (slug,),
        ).fetchone()

    def create_seen(
        self,
        slug: str,
        gift_id: int | None,
        owner_id: int | None,
        offer_min_stars: int | None,
        random_id: int,
    ) -> None:
        now = int(time.time())

        self.conn.execute(
            """
            INSERT OR IGNORE INTO gifts (
                slug,
                gift_id,
                owner_id,
                offer_min_stars,
                random_id,
                status,
                first_seen,
                last_update,
                error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                slug,
                gift_id,
                owner_id,
                offer_min_stars,
                random_id,
                "seen",
                now,
                now,
            ),
        )

        self.conn.commit()

    def mark_attempt(
        self,
        slug: str,
        gift_id: int | None,
        owner_id: int | None,
        offer_min_stars: int | None,
        random_id: int,
    ) -> None:
        now = int(time.time())

        self.conn.execute(
            """
            UPDATE gifts
            SET
                gift_id = ?,
                owner_id = ?,
                offer_min_stars = ?,
                random_id = ?,
                status = ?,
                last_update = ?,
                error = NULL
            WHERE slug = ?
            """,
            (
                gift_id,
                owner_id,
                offer_min_stars,
                random_id,
                "offer_attempted",
                now,
                slug,
            ),
        )

        self.conn.commit()

    def mark_success(self, slug: str) -> None:
        self.conn.execute(
            """
            UPDATE gifts
            SET
                status = 'offered',
                last_update = ?,
                error = NULL
            WHERE slug = ?
            """,
            (
                int(time.time()),
                slug,
            ),
        )

        self.conn.commit()

    def mark_failed(self, slug: str, message: str) -> None:
        self.conn.execute(
            """
            UPDATE gifts
            SET
                status = 'failed',
                last_update = ?,
                error = ?
            WHERE slug = ?
            """,
            (
                int(time.time()),
                message[:1000],
                slug,
            ),
        )

        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# ============================================================
# AUTH
# ============================================================

async def interactive_login(client: TelegramClient) -> bool:
    """
    Explicit first-run login.

    We don't call client.start() blindly because client.start()
    can call input() internally and produce EOFError in a Docker
    container without stdin.
    """

    if not sys.stdin.isatty():
        error(
            "Первый запуск требует интерактивный stdin. "
            "Запусти контейнер с интерактивным stdin, например "
            "`docker run -it ...`, либо один раз создай buyer.session "
            "локально и перенеси session-файл в контейнер."
        )
        return False

    try:
        phone = input("Введите номер телефона Telegram: ").strip()

        if not phone:
            error("Номер телефона не указан.")
            return False

        await client.send_code_request(phone)

        code = input("Введите код из Telegram: ").strip()

        if not code:
            error("Код авторизации не указан.")
            return False

        try:
            await client.sign_in(
                phone=phone,
                code=code,
            )

        except SessionPasswordNeededError:
            password = input("Введите пароль двухфакторной аутентификации: ")

            if not password:
                error("Пароль 2FA не указан.")
                return False

            await client.sign_in(password=password)

        if not await client.is_user_authorized():
            error("Telegram не подтвердил авторизацию.")
            return False

        success("Авторизация выполнена.")
        return True

    except EOFError:
        error(
            "stdin недоступен. Первый запуск необходимо выполнить "
            "в интерактивном терминале."
        )
        return False

    except KeyboardInterrupt:
        error("Авторизация прервана пользователем.")
        return False

    except Exception as exc:
        error(f"Ошибка авторизации: {type(exc).__name__}: {exc}")
        return False


async def connect_and_authorize(client: TelegramClient) -> bool:
    try:
        await client.connect()

        if await client.is_user_authorized():
            me = await client.get_me()

            username = (
                f"@{me.username}"
                if getattr(me, "username", None)
                else str(me.id)
            )

            success(f"Session загружена: {username}")
            return True

        return await interactive_login(client)

    except AuthKeyDuplicatedError:
        error(
            "Telegram отклонил session: AuthKeyDuplicatedError. "
            "Не используй один и тот же buyer.session одновременно "
            "в нескольких независимых инстансах."
        )
        return False

    except Exception as exc:
        error(f"Ошибка подключения: {type(exc).__name__}: {exc}")
        return False


# ============================================================
# TELEGRAM HELPERS
# ============================================================

def get_peer_numeric_id(peer: Any) -> int | None:
    if peer is None:
        return None

    try:
        return int(utils.get_peer_id(peer))
    except Exception:
        return None


async def resolve_owner_input_peer(
    client: TelegramClient,
    owner_peer: Any,
):
    """
    Converts Telegram Peer/User/Channel object to an InputPeer
    suitable for payments.sendStarGiftOffer.
    """

    if owner_peer is None:
        return None

    try:
        return await client.get_input_entity(owner_peer)
    except Exception:
        pass

    numeric_id = get_peer_numeric_id(owner_peer)

    if numeric_id is None:
        return None

    try:
        return await client.get_input_entity(numeric_id)
    except Exception:
        return None


def is_collectible(gift: Any) -> bool:
    """
    A resale result representing a collectible has the fields
    defined by Telegram's starGiftUnique constructor.
    """

    return (
        gift is not None
        and getattr(gift, "slug", None) is not None
        and getattr(gift, "owner_id", None) is not None
        and hasattr(gift, "offer_min_stars")
    )


# ============================================================
# GET BASE GIFT TYPES
# ============================================================

async def get_resellable_base_gift_ids(
    client: TelegramClient,
) -> list[int]:
    """
    Telegram's getResaleStarGifts requires a base gift_id.

    getStarGifts returns the catalogue of base gifts.
    Gifts with availability_resale set are the types for which
    Telegram indicates resale availability.
    """

    for attempt in range(REQUEST_RETRIES):
        try:
            result = await client(
                GetStarGiftsRequest(
                    hash=0,
                )
            )

            gifts = getattr(result, "gifts", []) or []

            ids = []

            for gift in gifts:
                gift_id = getattr(gift, "id", None)

                if gift_id is None:
                    continue

                availability_resale = getattr(
                    gift,
                    "availability_resale",
                    None,
                )

                # Telegram uses this optional field to indicate
                # that gifts of this type can exist on resale.
                if availability_resale is not None:
                    ids.append(int(gift_id))

            # Some environments/versions may not expose the
            # optional field exactly as expected. In that case,
            # keep every base gift with upgrade variants.
            if not ids:
                for gift in gifts:
                    gift_id = getattr(gift, "id", None)

                    if gift_id is None:
                        continue

                    upgrade_variants = getattr(
                        gift,
                        "upgrade_variants",
                        None,
                    )

                    if upgrade_variants:
                        ids.append(int(gift_id))

            return sorted(set(ids))

        except FloodWaitError as exc:
            wait_log(
                f"FloodWait при получении списка gift types: "
                f"{exc.seconds} секунд"
            )
            await asyncio.sleep(exc.seconds + 1)

        except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
            if attempt == REQUEST_RETRIES - 1:
                raise

            wait_log(
                f"Сеть временно недоступна: {exc}. "
                f"Повтор через {NETWORK_RETRY_DELAY} сек."
            )
            await asyncio.sleep(NETWORK_RETRY_DELAY)

        except RPCError:
            raise

    return []


# ============================================================
# RESALE SCANNING
# ============================================================

async def fetch_resale_page(
    client: TelegramClient,
    gift_id: int,
    offset: str = "",
):
    """
    Fetch one page from Telegram's official collectible resale
    catalogue.
    """

    for attempt in range(REQUEST_RETRIES):
        try:
            result = await client(
                GetResaleStarGiftsRequest(
                    sort_by_price=False,
                    sort_by_num=False,
                    for_craft=False,
                    stars_only=True,
                    attributes_hash=None,
                    gift_id=gift_id,
                    attributes=None,
                    offset=offset,
                    limit=PAGE_LIMIT,
                )
            )

            return result

        except FloodWaitError:
            raise

        except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
            if attempt == REQUEST_RETRIES - 1:
                raise

            wait_log(
                f"Временная ошибка сети: {exc}. "
                f"Повтор через {NETWORK_RETRY_DELAY} сек."
            )

            await asyncio.sleep(NETWORK_RETRY_DELAY)

        except RPCError:
            raise

    return None


async def scan_gift_type(
    client: TelegramClient,
    db: Database,
    gift_id: int,
) -> int:
    """
    Scan all currently returned resale pages for one base gift type.
    """

    offset = ""
    processed = 0

    while True:
        result = await fetch_resale_page(
            client,
            gift_id,
            offset,
        )

        if result is None:
            break

        gifts = getattr(result, "gifts", []) or []
        users = getattr(result, "users", []) or []

        # Build user cache from the users vector returned by Telegram.
        users_by_id = {}

        for user in users:
            try:
                users_by_id[int(user.id)] = user
            except Exception:
                pass

        for gift in gifts:
            if not is_collectible(gift):
                continue

            slug = getattr(gift, "slug", None)

            if not slug:
                continue

            owner_peer = getattr(gift, "owner_id", None)

            if owner_peer is None:
                continue

            owner_id = get_peer_numeric_id(owner_peer)

            # If Telegram gave us PeerUser and also supplied the
            # complete user object, prefer the actual User object.
            owner_user = None

            try:
                raw_user_id = getattr(owner_peer, "user_id", None)

                if raw_user_id is not None:
                    owner_user = users_by_id.get(int(raw_user_id))
            except Exception:
                owner_user = None

            if owner_user is not None:
                owner_id = int(owner_user.id)

            offer_min = getattr(
                gift,
                "offer_min_stars",
                None,
            )

            if offer_min is None:
                # Telegram only allows an offer if this field exists.
                continue

            try:
                offer_min = int(offer_min)
            except Exception:
                continue

            # Generate random_id once and persist it before the RPC.
            # If the network dies after Telegram accepted the request,
            # the same random_id can safely be reused.
            existing = db.get(slug)

            if existing is not None:
                status = existing[5]

                # Never intentionally submit a second offer for the
                # same slug after a previous successful/attempted offer.
                if status in (
                    "offer_attempted",
                    "offered",
                ):
                    continue

                random_id = int(existing[4])
            else:
                random_id = random.getrandbits(63)

                db.create_seen(
                    slug=slug,
                    gift_id=int(getattr(gift, "gift_id", gift_id)),
                    owner_id=owner_id,
                    offer_min_stars=offer_min,
                    random_id=random_id,
                )

            nft_log(f"Found collectible gift: {slug}")
            nft_log(f"Owner: {owner_id}")
            nft_log(f"Offer minimum: {offer_min} ⭐")

            # Telegram explicitly says offer_min_stars is the minimum
            # Stars offer accepted for that collectible.
            if OFFER_STARS < offer_min:
                info(
                    f"Skip {slug}: minimum offer is "
                    f"{offer_min} ⭐, configured offer is "
                    f"{OFFER_STARS} ⭐."
                )
                db.mark_failed(
                    slug,
                    f"Minimum offer is {offer_min}, "
                    f"configured amount is {OFFER_STARS}",
                )
                continue

            # At this point Telegram's collectible object tells us:
            # - it is unique/collectible
            # - it has an owner
            # - an offer minimum exists
            # - 125 Stars meets that minimum
            #
            # Resolve the current owner to InputPeer.
            peer = await resolve_owner_input_peer(
                client,
                owner_user if owner_user is not None else owner_peer,
            )

            if peer is None:
                error(
                    f"Не удалось получить InputPeer владельца "
                    f"для {slug}."
                )
                db.mark_failed(
                    slug,
                    "Could not resolve owner InputPeer",
                )
                continue

            # Mark before sending to prevent another polling pass
            # from submitting another offer concurrently.
            db.mark_attempt(
                slug=slug,
                gift_id=int(getattr(gift, "gift_id", gift_id)),
                owner_id=owner_id,
                offer_min_stars=offer_min,
                random_id=random_id,
            )

            offer_log(
                f"Sending {OFFER_STARS} ⭐ "
                f"for {slug} to {owner_id}"
            )

            try:
                price = StarsAmount(
                    stars=OFFER_STARS,
                    nanos=0,
                )

                await client(
                    SendStarGiftOfferRequest(
                        peer=peer,
                        slug=slug,
                        price=price,
                        duration=OFFER_DURATION,
                        random_id=random_id,
                    )
                )

                db.mark_success(slug)

                success(
                    f"Offer sent: {slug} | "
                    f"{OFFER_STARS} ⭐ | "
                    f"owner={owner_id}"
                )

            except FloodWaitError as exc:
                # The attempt was already registered in SQLite.
                # We do NOT generate another random_id.
                # If Telegram did not accept the request, the next
                # manual/restart strategy can reuse the same ID.
                wait_log(
                    f"FloodWait: {exc.seconds} seconds"
                )
                await asyncio.sleep(exc.seconds + 1)

            except RPCError as exc:
                error(
                    f"Offer RPC error for {slug}: "
                    f"{type(exc).__name__}: {exc}"
                )

                db.mark_failed(
                    slug,
                    f"{type(exc).__name__}: {exc}",
                )

            except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
                # Do not immediately mark the transaction as successful.
                # The random_id is persisted, so the same request ID can
                # be used later if the implementation is extended to retry
                # uncertain network outcomes.
                error(
                    f"Network error while sending offer "
                    f"for {slug}: {exc}"
                )

                db.mark_failed(
                    slug,
                    f"Network error: {exc}",
                )

            processed += 1

        next_offset = getattr(
            result,
            "next_offset",
            None,
        )

        if not next_offset:
            break

        if next_offset == offset:
            break

        offset = next_offset

        await asyncio.sleep(REQUEST_DELAY)

    return processed


# ============================================================
# MAIN SCANNER
# ============================================================

async def scanner_loop(
    client: TelegramClient,
    db: Database,
) -> None:

    info("Получение списка типов collectible gifts...")

    while True:
        try:
            gift_ids = await get_resellable_base_gift_ids(client)

            if not gift_ids:
                info(
                    "Telegram не вернул типы gift с resale availability. "
                    f"Следующая проверка через {POLL_INTERVAL} сек."
                )

                await asyncio.sleep(POLL_INTERVAL)
                continue

            info(
                f"Доступно для проверки base gift types: "
                f"{len(gift_ids)}"
            )

            for gift_id in gift_ids:
                try:
                    await scan_gift_type(
                        client,
                        db,
                        gift_id,
                    )

                except FloodWaitError as exc:
                    wait_log(
                        f"FloodWait: {exc.seconds} секунд"
                    )
                    await asyncio.sleep(exc.seconds + 1)

                except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
                    error(
                        f"Network error while scanning gift "
                        f"{gift_id}: {exc}"
                    )

                    await asyncio.sleep(
                        NETWORK_RETRY_DELAY
                    )

                except RPCError as exc:
                    error(
                        f"RPC error while scanning gift "
                        f"{gift_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )

                except Exception as exc:
                    error(
                        f"Unexpected error while scanning "
                        f"gift {gift_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )

                await asyncio.sleep(REQUEST_DELAY)

            wait_log(
                f"Scan complete. Next scan in "
                f"{POLL_INTERVAL} seconds."
            )

            await asyncio.sleep(POLL_INTERVAL)

        except FloodWaitError as exc:
            wait_log(
                f"Global FloodWait: {exc.seconds} seconds"
            )
            await asyncio.sleep(exc.seconds + 1)

        except AuthKeyDuplicatedError:
            error(
                "Session была использована в другом месте. "
                "Остановка."
            )
            return

        except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
            error(
                f"Connection error: {exc}. "
                f"Reconnect in {NETWORK_RETRY_DELAY} sec."
            )

            try:
                await client.disconnect()
            except Exception:
                pass

            await asyncio.sleep(NETWORK_RETRY_DELAY)

            try:
                await client.connect()

                if not await client.is_user_authorized():
                    error("Session больше не авторизована.")
                    return

                success("Reconnect successful.")

            except Exception as reconnect_exc:
                error(
                    f"Reconnect failed: "
                    f"{type(reconnect_exc).__name__}: "
                    f"{reconnect_exc}"
                )

                await asyncio.sleep(
                    NETWORK_RETRY_DELAY
                )

        except RPCError as exc:
            error(
                f"Global RPC error: "
                f"{type(exc).__name__}: {exc}"
            )

            await asyncio.sleep(
                NETWORK_RETRY_DELAY
            )

        except Exception as exc:
            error(
                f"Scanner exception: "
                f"{type(exc).__name__}: {exc}"
            )

            await asyncio.sleep(
                NETWORK_RETRY_DELAY
            )


# ============================================================
# ENTRY POINT
# ============================================================

async def main() -> None:
    if API_ID == 123456 or API_HASH == "YOUR_API_HASH":
        error(
            "Сначала укажи реальные API_ID и API_HASH "
            "в начале main.py."
        )
        return

    if not isinstance(API_ID, int):
        error("API_ID должен быть integer.")
        return

    db = Database(DATABASE_NAME)

    client = TelegramClient(
        SESSION_NAME,
        API_ID,
        API_HASH,
        connection=ConnectionTcpFull,
        auto_reconnect=True,
        connection_retries=5,
        retry_delay=5,
        request_retries=5,
    )

    try:
        authorized = await connect_and_authorize(client)

        if not authorized:
            return

        me = await client.get_me()

        info(
            f"Account ID: {me.id}"
        )

        info(
            "Telegram collectible gift monitor started."
        )

        info(
            f"Offer amount: {OFFER_STARS} ⭐"
        )

        info(
            f"Offer duration: {OFFER_DURATION} seconds"
        )

        info(
            "Monitoring official Telegram resale catalogue."
        )

        await scanner_loop(
            client,
            db,
        )

    except KeyboardInterrupt:
        info("Остановка пользователем.")

    except AuthKeyDuplicatedError:
        error(
            "AuthKeyDuplicatedError: "
            "не используй одну session одновременно "
            "в нескольких процессах."
        )

    except Exception as exc:
        error(
            f"Fatal error: "
            f"{type(exc).__name__}: {exc}"
        )

    finally:
        db.close()

        try:
            await client.disconnect()
        except Exception:
            pass

        info("Stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        info("Stopped by user.")
```
