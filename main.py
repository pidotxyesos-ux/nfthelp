from telethon import TelegramClient, functions, types
import random

API_ID = 35175774
API_HASH = "919166219f6c1336e4136ed120d01306"

client = TelegramClient("buyer_account", API_ID, API_HASH)

async def make_offer(owner, slug):
    await client(
        functions.payments.SendStarGiftOfferRequest(
            peer=owner,
            slug=slug,
            price=types.StarsAmount(
                amount=125,
                nanos=0
            ),
            duration=21600,  # 6 часов
            random_id=random.getrandbits(64)
        )
    )

async def main():
    # owner — Telegram ID/peer владельца
    # slug — slug конкретного collectible gift
    await make_offer(
        owner="username_or_user_id",
        slug="COLLECTIBLE-GIFT-SLUG"
    )

with client:
    client.loop.run_until_complete(main())
