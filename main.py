import asyncio
import random
import sqlite3
import sys
import time

from telethon import TelegramClient, events, types, utils
from telethon.errors import (
    FloodWaitError,
    RPCError,
    SessionPasswordNeededError,
    AuthKeyDuplicatedError,
)
from telethon.network.connection.connection import ConnectionTcpFull
from telethon.tl.functions.payments import SendStarGiftOfferRequest
from telethon.tl.types import StarsAmount


# ============================================================
# CONFIG
# ============================================================

API_ID = 35175774
API_HASH = "919166219f6c1336e4136ed120d01306"

OFFER_STARS = 125

SESSION_NAME = "buyer"
DATABASE_NAME = "buyer.sqlite3"

# Telegram production values:
# 21600  = 6 hours
# 43200  = 12 hours
# 86400  = 24 hours
# 129600 = 36 hours
# 172800 = 48 hours
# 259200 = 72 hours
OFFER_DURATION = 21600

# Only newly created collectible events.
# upgrade = regular gift upgraded into collectible
# craft   = collectible created by crafting
WATCH_UPGRADES = True
WATCH_CRAFTS = True


# ============================================================
# LOGGING
# ============================================================

def log(level, text):
    print(
        f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"[{level}] {text}",
        flush=True,
    )


def info(text):
    log("INFO", text)


def nft(text):
    log("NFT", text)


def offer(text):
    log("OFFER", text)


def success(text):
    log("SUCCESS", text)


def error(text):
    log("ERROR", text)


def wait_log(text):
    log("WAIT", text)


# ============================================================
# DATABASE
# ============================================================

class Database:
    def __init__(self, filename):
        self.conn = sqlite3.connect(
            filename,
            check_same_thread=False,
        )

        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_gifts (
                slug TEXT PRIMARY KEY,
                gift_id INTEGER,
                owner_id INTEGER,
                offer_min_stars INTEGER,
                random_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                error TEXT
            )
            """
        )

        self.conn.commit()

        # Prevent two Telegram updates from processing
        # the same NFT simultaneously.
        self.lock = asyncio.Lock()

    async def claim(
        self,
        slug,
        gift_id,
        owner_id,
        offer_min_stars,
        random_id,
    ):
        """
        Atomically claim a slug.

        Returns True only for the first processing attempt.
        """

        async with self.lock:
            now = int(time.time())

            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO processed_gifts (
                    slug,
                    gift_id,
                    owner_id,
                    offer_min_stars,
                    random_id,
                    status,
                    created_at,
                    updated_at,
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
                    "processing",
                    now,
                    now,
                ),
            )

            self.conn.commit()

            return cursor.rowcount == 1

    def mark_success(self, slug):
        self.conn.execute(
            """
            UPDATE processed_gifts
            SET
                status = 'offer_sent',
                updated_at = ?,
                error = NULL
            WHERE slug = ?
            """,
            (
                int(time.time()),
                slug,
            ),
        )

        self.conn.commit()

    def mark_failed(self, slug, message):
        self.conn.execute(
            """
            UPDATE processed_gifts
            SET
                status = 'failed',
                updated_at = ?,
                error = ?
            WHERE slug = ?
            """,
            (
                int(time.time()),
                message[:2000],
                slug,
            ),
        )

        self.conn.commit()

    def close(self):
        self.conn.close()


# ============================================================
# AUTH
# ============================================================

async def first_login(client):
    """
    Interactive first login.

    buyer.session is then reused automatically.
    """

    if not sys.stdin.isatty():
        error(
            "Первый запуск требует интерактивный stdin.\n"
            "На хостинге проще один раз авторизовать аккаунт "
            "локально, получить buyer.session и загрузить "
            "buyer.session рядом с main.py."
        )
        return False

    try:
        phone = input(
            "Введите номер телефона Telegram: "
        ).strip()

        if not phone:
            error("Номер телефона пустой.")
            return False

        await client.send_code_request(phone)

        code = input(
            "Введите код из Telegram: "
        ).strip()

        if not code:
            error("Код пустой.")
            return False

        try:
            await client.sign_in(
                phone=phone,
                code=code,
            )

        except SessionPasswordNeededError:
            password = input(
                "Введите пароль 2FA: "
            )

            await client.sign_in(
                password=password
            )

        if not await client.is_user_authorized():
            error("Telegram не подтвердил авторизацию.")
            return False

        success("Авторизация успешно выполнена.")
        return True

    except EOFError:
        error(
            "stdin недоступен.\n"
            "Авторизуй buyer.session интерактивно "
            "и загрузи его на хостинг."
        )
        return False

    except KeyboardInterrupt:
        error("Авторизация отменена.")
        return False

    except Exception as exc:
        error(
            f"Ошибка авторизации: "
            f"{type(exc).__name__}: {exc}"
        )
        return False


async def connect(client):
    try:
        await client.connect()

        if await client.is_user_authorized():
            me = await client.get_me()

            username = (
                f"@{me.username}"
                if getattr(me, "username", None)
                else str(me.id)
            )

            success(
                f"Session загружена. Аккаунт: {username}"
            )

            return True

        return await first_login(client)

    except AuthKeyDuplicatedError:
        error(
            "buyer.session используется одновременно "
            "в другом месте."
        )
        return False

    except Exception as exc:
        error(
            f"Ошибка подключения: "
            f"{type(exc).__name__}: {exc}"
        )
        return False


# ============================================================
# PEER
# ============================================================

async def get_owner_peer(client, owner_id):
    """
    starGiftUnique.owner_id is a Peer.

    sendStarGiftOffer requires InputPeer.
    """

    if owner_id is None:
        return None

    try:
        return await client.get_input_entity(owner_id)
    except Exception:
        pass

    try:
        numeric_id = utils.get_peer_id(owner_id)
        return await client.get_input_entity(numeric_id)
    except Exception:
        return None


def get_numeric_owner_id(owner_id):
    if owner_id is None:
        return None

    try:
        return utils.get_peer_id(owner_id)
    except Exception:
        return None


# ============================================================
# OFFER
# ============================================================

async def send_offer(
    client,
    db,
    gift,
):
    """
    Send official Telegram purchase offer.
    """

    # --------------------------------------------------------
    # Basic validation
    # --------------------------------------------------------

    slug = getattr(gift, "slug", None)

    if not slug:
        error("NFT без slug. Пропуск.")
        return

    owner = getattr(gift, "owner_id", None)

    if owner is None:
        error(
            f"{slug}: owner_id отсутствует. "
            f"Offer невозможен."
        )
        return

    owner_numeric_id = get_numeric_owner_id(owner)

    if owner_numeric_id is None:
        error(
            f"{slug}: невозможно определить owner_id."
        )
        return

    # If offer_min_stars doesn't exist, Telegram does not
    # advertise that purchase offers are available.
    offer_min = getattr(
        gift,
        "offer_min_stars",
        None,
    )

    if offer_min is None:
        info(
            f"{slug}: offer для этого NFT "
            f"не разрешён Telegram."
        )
        return

    try:
        offer_min = int(offer_min)
    except Exception:
        error(
            f"{slug}: некорректный offer_min_stars."
        )
        return

    # --------------------------------------------------------
    # Check price
    # --------------------------------------------------------

    if OFFER_STARS < offer_min:
        info(
            f"{slug}: minimum offer = "
            f"{offer_min} ⭐, "
            f"наш offer = {OFFER_STARS} ⭐. Skip."
        )
        return

    # --------------------------------------------------------
    # Skip burned collectibles
    # --------------------------------------------------------

    if getattr(gift, "burned", False):
        info(
            f"{slug}: NFT уже burned. Skip."
        )
        return

    # --------------------------------------------------------
    # Skip TON-only gifts
    # --------------------------------------------------------

    if getattr(gift, "resale_ton_only", False):
        info(
            f"{slug}: gift помечен resale_ton_only. Skip."
        )
        return

    # --------------------------------------------------------
    # Generate persistent random_id.
    #
    # Telegram uses this to deduplicate the same offer
    # in case of network problems.
    # --------------------------------------------------------

    random_id = random.getrandbits(63)

    # --------------------------------------------------------
    # Claim NFT in SQLite.
    #
    # If another update contains the same slug,
    # it will be ignored.
    # --------------------------------------------------------

    claimed = await db.claim(
        slug=slug,
        gift_id=int(getattr(gift, "gift_id", 0)),
        owner_id=int(owner_numeric_id),
        offer_min_stars=offer_min,
        random_id=random_id,
    )

    if not claimed:
        info(
            f"{slug}: уже обработан. Skip."
        )
        return

    nft(
        f"New collectible detected: {slug}"
    )

    nft(
        f"Owner: {owner_numeric_id}"
    )

    nft(
        f"Minimum offer: {offer_min} ⭐"
    )

    # --------------------------------------------------------
    # Resolve owner to InputPeer
    # --------------------------------------------------------

    peer = await get_owner_peer(
        client,
        owner,
    )

    if peer is None:
        error(
            f"{slug}: невозможно получить InputPeer владельца."
        )

        db.mark_failed(
            slug,
            "Unable to resolve owner InputPeer",
        )

        return

    # --------------------------------------------------------
    # SEND OFFER
    # --------------------------------------------------------

    offer(
        f"Sending {OFFER_STARS} ⭐ offer "
        f"for {slug} to {owner_numeric_id}"
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
            f"Offer sent successfully: "
            f"{slug} → {OFFER_STARS} ⭐"
        )

    except FloodWaitError as exc:
        wait_log(
            f"FloodWait: {exc.seconds} seconds"
        )

        await asyncio.sleep(
            exc.seconds + 1
        )

        # We intentionally DO NOT generate another
        # random_id. The original random_id is stored
        # in SQLite.
        #
        # We also don't blindly resend because the RPC
        # may have been accepted before FloodWait/network
        # interruption.

        db.mark_failed(
            slug,
            f"FloodWait: {exc.seconds}",
        )

    except RPCError as exc:
        error(
            f"Offer failed for {slug}: "
            f"{type(exc).__name__}: {exc}"
        )

        db.mark_failed(
            slug,
            f"{type(exc).__name__}: {exc}",
        )

    except (
        OSError,
        ConnectionError,
        asyncio.TimeoutError,
    ) as exc:
        error(
            f"Network error while sending offer "
            f"for {slug}: {exc}"
        )

        db.mark_failed(
            slug,
            f"Network error: {exc}",
        )

    except Exception as exc:
        error(
            f"Unexpected offer error for {slug}: "
            f"{type(exc).__name__}: {exc}"
        )

        db.mark_failed(
            slug,
            f"{type(exc).__name__}: {exc}",
        )


# ============================================================
# NFT DETECTION
# ============================================================

async def process_message(
    client,
    db,
    message,
):
    """
    Detect messageActionStarGiftUnique.

    We specifically watch events representing creation of
    a collectible:
        upgrade=True
        craft=True

    Transfers/saves/refunds are ignored.
    """

    action = getattr(
        message,
        "action",
        None,
    )

    if action is None:
        return

    if not isinstance(
        action,
        types.MessageActionStarGiftUnique,
    ):
        return

    # --------------------------------------------------------
    # Determine why the collectible appeared.
    # --------------------------------------------------------

    is_upgrade = bool(
        getattr(action, "upgrade", False)
    )

    is_craft = bool(
        getattr(action, "craft", False)
    )

    if is_upgrade and not WATCH_UPGRADES:
        return

    if is_craft and not WATCH_CRAFTS:
        return

    # If neither flag is set, this is most likely a transfer,
    # assignment, save, refund, etc., rather than creation.
    if not is_upgrade and not is_craft:
        return

    gift = getattr(
        action,
        "gift",
        None,
    )

    if gift is None:
        return

    # --------------------------------------------------------
    # Confirm collectible.
    #
    # Telegram's unique collectible has slug + owner_id.
    # --------------------------------------------------------

    slug = getattr(
        gift,
        "slug",
        None,
    )

    owner_id = getattr(
        gift,
        "owner_id",
        None,
    )

    if not slug:
        return

    if owner_id is None:
        # Telegram can represent TON-owned/hosted gifts
        # without a Telegram owner_id. Such a gift cannot
        # be targeted by sendStarGiftOffer.
        info(
            f"{slug}: owner_id отсутствует. "
            f"Offer пропущен."
        )
        return

    if is_upgrade:
        nft(
            f"Detected NEW collectible by upgrade: "
            f"{slug}"
        )

    elif is_craft:
        nft(
            f"Detected NEW collectible by craft: "
            f"{slug}"
        )

    await send_offer(
        client,
        db,
        gift,
    )


# ============================================================
# EVENT HANDLER
# ============================================================

def install_handlers(
    client,
    db,
):
    @client.on(events.NewMessage)
    async def new_message_handler(event):
        try:
            await process_message(
                client,
                db,
                event.message,
            )

        except FloodWaitError as exc:
            wait_log(
                f"FloodWait in event handler: "
                f"{exc.seconds} seconds"
            )

            await asyncio.sleep(
                exc.seconds + 1
            )

        except (
            OSError,
            ConnectionError,
            asyncio.TimeoutError,
        ) as exc:
            error(
                f"Temporary network error: {exc}"
            )

        except RPCError as exc:
            error(
                f"Telegram RPC error: "
                f"{type(exc).__name__}: {exc}"
            )

        except Exception as exc:
            error(
                f"Event handler error: "
                f"{type(exc).__name__}: {exc}"
            )


# ============================================================
# CONNECTION WATCHDOG
# ============================================================

async def connection_watchdog(client):
    while True:
        try:
            if not client.is_connected():
                wait_log(
                    "Telegram disconnected. Reconnecting..."
                )

                await client.connect()

                if await client.is_user_authorized():
                    success(
                        "Telegram connection restored."
                    )
                else:
                    error(
                        "Session is no longer authorized."
                    )
                    return

        except FloodWaitError as exc:
            wait_log(
                f"Reconnect FloodWait: "
                f"{exc.seconds} seconds"
            )

            await asyncio.sleep(
                exc.seconds + 1
            )

        except Exception as exc:
            error(
                f"Reconnect error: "
                f"{type(exc).__name__}: {exc}"
            )

            await asyncio.sleep(5)

        await asyncio.sleep(5)


# ============================================================
# MAIN
# ============================================================

async def main():
    if API_ID == 123456:
        error(
            "Укажи настоящий API_ID в начале main.py."
        )
        return

    if API_HASH == "YOUR_API_HASH":
        error(
            "Укажи настоящий API_HASH в начале main.py."
        )
        return

    if OFFER_STARS <= 0:
        error(
            "OFFER_STARS должен быть больше 0."
        )
        return

    if OFFER_DURATION not in (
        21600,
        43200,
        86400,
        129600,
        172800,
        259200,
    ):
        error(
            "Некорректная OFFER_DURATION."
        )
        return

    db = Database(
        DATABASE_NAME
    )

    client = TelegramClient(
        SESSION_NAME,
        API_ID,
        API_HASH,
        connection=ConnectionTcpFull,
        auto_reconnect=True,
        connection_retries=10,
        retry_delay=5,
        request_retries=5,
    )

    try:
        if not await connect(client):
            return

        me = await client.get_me()

        info(
            f"Logged in as: "
            f"{getattr(me, 'username', None) or me.id}"
        )

        info(
            f"Offer: {OFFER_STARS} ⭐"
        )

        info(
            f"Offer duration: "
            f"{OFFER_DURATION} seconds"
        )

        info(
            "NFT auto-offer monitor started."
        )

        info(
            "Waiting for new collectible "
            "upgrade/craft events..."
        )

        install_handlers(
            client,
            db,
        )

        watchdog = asyncio.create_task(
            connection_watchdog(client)
        )

        try:
            await client.run_until_disconnected()

        finally:
            watchdog.cancel()

            try:
                await watchdog
            except asyncio.CancelledError:
                pass

    except AuthKeyDuplicatedError:
        error(
            "buyer.session используется "
            "в другом процессе."
        )

    except KeyboardInterrupt:
        info("Stopped by user.")

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


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
