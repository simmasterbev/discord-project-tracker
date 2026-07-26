"""Small live Discord check.

Run manually when the bot is invited to a test server:
    python discord_channel_smoke_test.py

It sends one clearly labelled message and edits it once. It does not create
projects, tasks, trees, or database records.
"""

import asyncio
import logging
import os

import discord


def clean(value: str) -> str:
    return "".join(c for c in value if 32 <= ord(c) <= 126).strip()


async def run(token: str, channel_id: int) -> None:
    client = discord.Client(intents=discord.Intents.default())
    try:
        await client.login(token)
        channel = await client.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise RuntimeError("That ID is not a text channel or thread.")

        message = await channel.send("🧪 Progress Tracker smoke test: sending works.")
        await message.edit(content="✅ Progress Tracker smoke test passed: send + edit work.")
        print(f"PASS: sent and edited message {message.id} in #{channel.name}.")
    finally:
        await client.close()


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    token = clean(os.environ.get("DISCORD_TOKEN") or input("Discord bot token: "))
    raw_channel = clean(input("Discord channel ID: "))
    if not token or not raw_channel.isdigit():
        raise SystemExit("A token and numeric channel ID are required.")
    try:
        asyncio.run(run(token, int(raw_channel)))
    except discord.Forbidden:
        raise SystemExit("FAIL: the bot cannot access or send in that channel.")
    except discord.NotFound:
        raise SystemExit("FAIL: that channel was not found.")
    except discord.LoginFailure:
        raise SystemExit("FAIL: the token was rejected by Discord.")


if __name__ == "__main__":
    main()
