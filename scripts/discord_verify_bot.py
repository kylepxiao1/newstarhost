import asyncio
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import discord
import httpx
from TikTokLive import TikTokLiveClient
from TikTokLive.client.web.web_settings import WebDefaults
from discord.ext import commands

from utils import _env


logger = logging.getLogger("discord-verify-bot")


def _env_int(key: str, default: int = 0) -> int:
    value = _env(key, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    value = _env(key, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


class _DiscordReconnectNoiseFilter(logging.Filter):
    _TRANSIENT_EXC_NAMES = {
        "TimeoutError",
        "CancelledError",
        "ClientConnectorError",
        "ClientConnectionError",
        "ClientOSError",
        "ServerDisconnectedError",
    }

    def filter(self, record: logging.LogRecord) -> bool:
        logger_name = str(record.name or "")
        if not logger_name.startswith("discord."):
            return True
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg or "")
        if "Attempting a reconnect in" not in message:
            return True
        if not record.exc_info or len(record.exc_info) < 2:
            return True
        exc = record.exc_info[1]
        return type(exc).__name__ not in self._TRANSIENT_EXC_NAMES


def _install_discord_reconnect_noise_filter() -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(_DiscordReconnectNoiseFilter())


def _parse_handles_csv(value: str) -> list[str]:
    parts = re.split(r"[,\s]+", str(value or "").strip())
    out: list[str] = []
    seen: set[str] = set()
    for raw in parts:
        handle = _normalize_handle(raw)
        if not handle or handle in seen:
            continue
        seen.add(handle)
        out.append(handle)
    return out


def _pick_first(*values: str) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _normalize_handle(value: str) -> str:
    return str(value or "").strip().lstrip("@").lower()


def _is_valid_email(value: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value.strip()))


@dataclass
class PendingSubmission:
    request_id: str
    guild_id: int
    user_id: int
    discord_username: str
    tiktok_handle: str
    email: str
    phone: str
    date_of_birth: str
    favorite_wildcardz_member: str
    submitted_at: str
    donation_target_handle: str = "wildcard_boys"
    total_donated_diamonds: Optional[int] = None
    donation_lookup_error: str = ""

    def to_supabase_row(self) -> dict:
        return {
            "discord_user_id": str(self.user_id),
            "discord_username": self.discord_username,
            "tiktok_handle": self.tiktok_handle,
            "email": self.email,
            "phone": self.phone,
            "date_of_birth": self.date_of_birth,
            "favorite_wildcardz_member": self.favorite_wildcardz_member,
            "submitted_at": self.submitted_at,
        }

    def to_embed(self, status: str, reviewed_by: str = "", reason: str = "") -> discord.Embed:
        color = discord.Color.orange()
        if status.lower() == "approved":
            color = discord.Color.green()
        elif status.lower() == "rejected":
            color = discord.Color.red()
        embed = discord.Embed(
            title="Fan Verification Submission",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Request ID", value=self.request_id, inline=False)
        embed.add_field(name="User", value=f"<@{self.user_id}> (`{self.user_id}`)", inline=False)
        embed.add_field(name="TikTok Handle", value=self.tiktok_handle, inline=False)
        embed.add_field(name="Email", value=self.email, inline=False)
        embed.add_field(name="Phone", value=self.phone, inline=False)
        embed.add_field(name="Date of Birth", value=self.date_of_birth, inline=False)
        embed.add_field(
            name="Favorite Wildcardz Member",
            value=self.favorite_wildcardz_member,
            inline=False,
        )
        donation_target = self.donation_target_handle.strip().lstrip("@") or "wildcard_boys"
        if self.total_donated_diamonds is None:
            donation_value = "Unavailable"
            if self.donation_lookup_error:
                err_text = self.donation_lookup_error.strip()
                if len(err_text) > 180:
                    err_text = err_text[:177] + "..."
                donation_value = f"Unavailable ({err_text})"
        else:
            donation_value = f"{self.total_donated_diamonds:,} diamonds"
        embed.add_field(
            name=f"Total Donated to @{donation_target}",
            value=donation_value,
            inline=False,
        )
        embed.add_field(name="Status", value=status, inline=False)
        if reviewed_by:
            embed.add_field(name="Reviewed By", value=reviewed_by, inline=False)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"Submitted at {self.submitted_at}")
        return embed


class SupabaseFanInfoStore:
    def __init__(self, table_name: str = "fan_info") -> None:
        project_id = _env("SUPABASE_PROJECT_ID", "").strip()
        configured_url = _env("SUPABASE_URL", "").strip()
        self._supabase_url = configured_url or (f"https://{project_id}.supabase.co" if project_id else "")
        self._supabase_key = _pick_first(
            _env("SUPABASE_SECRET_KEY", ""),
            _env("SUPABASE_SERVICE_ROLE_KEY", ""),
            _env("SUPABASE_SERVICE_ROLE", ""),
            _env("SUPABASE_API_KEY", ""),
        )
        self._table_name = table_name
        self._http: Optional[httpx.AsyncClient] = None
        if self._supabase_url and self._supabase_key:
            self._http = httpx.AsyncClient(
                base_url=self._supabase_url.rstrip("/") + "/rest/v1",
                timeout=httpx.Timeout(20.0),
                headers={
                    "apikey": self._supabase_key,
                    "Authorization": f"Bearer {self._supabase_key}",
                    "Content-Type": "application/json",
                },
            )
        else:
            logger.warning(
                "Supabase credentials not configured. Set SUPABASE_URL (or SUPABASE_PROJECT_ID) and SUPABASE_SECRET_KEY."
            )

    def is_ready(self) -> bool:
        return self._http is not None

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @staticmethod
    def _extract_missing_column(resp: httpx.Response) -> str:
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or "")
        if not message:
            message = str(resp.text or "")
        match = re.search(
            r"Could not find the '([A-Za-z0-9_]+)' column of '[^']+' in the schema cache",
            message,
        )
        if not match:
            return ""
        return match.group(1)

    @staticmethod
    def _to_int(value) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        try:
            text = str(value).strip()
        except Exception:
            return None
        if not text:
            return None
        try:
            return int(text)
        except Exception:
            try:
                return int(float(text))
            except Exception:
                return None

    @classmethod
    def _gift_row_diamond_amount(cls, row: dict) -> int:
        amount_value = cls._to_int(row.get("amount_value"))
        if amount_value is not None:
            return max(0, amount_value)
        diamond_count = cls._to_int(row.get("diamond_count"))
        if diamond_count is None:
            return 0
        repeat_count = cls._to_int(row.get("repeat_count"))
        combo_count = cls._to_int(row.get("combo_count"))
        multiplier = repeat_count if repeat_count is not None else combo_count
        if multiplier is None:
            multiplier = 1
        return max(0, diamond_count * max(1, multiplier))

    async def get_total_donations_to_target(
        self,
        *,
        donor_handle: str,
        target_tiktok_handle: str,
    ) -> tuple[Optional[int], str]:
        if self._http is None:
            return None, "Supabase unavailable"

        donor_raw = str(donor_handle or "").strip().lstrip("@")
        donor = _normalize_handle(donor_handle)
        target = _normalize_handle(target_tiktok_handle)
        if not donor:
            return 0, ""
        if not target:
            return None, "Missing target handle"

        page_size = 1000
        total = 0
        max_pages = 200
        donor_variants = []
        for variant in (donor_raw, donor):
            base = str(variant or "").strip()
            if not base:
                continue
            for candidate in (base, f"@{base}"):
                if candidate not in donor_variants:
                    donor_variants.append(candidate)
        seen_ids: set[int] = set()

        for donor_variant in donor_variants:
            offset = 0
            pages = 0
            while True:
                params = {
                    "select": "id,amount_value,diamond_count,repeat_count,combo_count",
                    "tiktok_username": f"eq.{target}",
                    "from_username": f"eq.{donor_variant}",
                    "order": "id.asc",
                }
                headers = {
                    "Range-Unit": "items",
                    "Range": f"{offset}-{offset + page_size - 1}",
                }
                try:
                    resp = await self._http.get("/gift_events", params=params, headers=headers)
                except Exception as exc:
                    return None, f"{type(exc).__name__}: {exc}"
                if not resp.is_success:
                    try:
                        payload = resp.json()
                    except Exception:
                        payload = resp.text
                    return None, f"status={resp.status_code}: {payload}"
                try:
                    rows = resp.json()
                except Exception as exc:
                    return None, f"JSON parse failed: {type(exc).__name__}: {exc}"
                if not isinstance(rows, list):
                    return None, "Unexpected gift_events response type"

                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    row_id = self._to_int(row.get("id"))
                    if row_id is not None:
                        if row_id in seen_ids:
                            continue
                        seen_ids.add(row_id)
                    total += self._gift_row_diamond_amount(row)

                rows_count = len(rows)
                if rows_count < page_size:
                    break
                offset += page_size
                pages += 1
                if pages >= max_pages:
                    return None, "Exceeded donation pagination limit"

        return total, ""

    async def insert_fan_info(self, row: dict) -> tuple[bool, str]:
        if self._http is None:
            return False, "Supabase client is not configured."

        payload = dict(row)
        for _ in range(8):
            if not payload:
                return False, "fan_info insert failed: no compatible columns left after schema checks."
            try:
                resp = await self._http.post(
                    f"/{self._table_name}",
                    json=payload,
                    headers={"Prefer": "return=representation"},
                )
            except Exception as exc:
                return False, f"Supabase request failed: {type(exc).__name__}: {exc}"
            if resp.is_success:
                try:
                    inserted_rows = resp.json()
                except Exception as exc:
                    return False, f"Supabase insert confirmation parse failed: {type(exc).__name__}: {exc}"
                if isinstance(inserted_rows, list) and len(inserted_rows) >= 1:
                    return True, ""
                return False, "Supabase insert did not return a row confirmation."
            missing_column = self._extract_missing_column(resp)
            if missing_column and missing_column in payload:
                payload.pop(missing_column, None)
                continue
            try:
                error_payload = resp.json()
            except Exception:
                error_payload = resp.text
            return False, f"Supabase status {resp.status_code}: {error_payload}"
        return False, "fan_info insert failed after retries."


class VerificationStartView(discord.ui.View):
    def __init__(self, bot: "FanVerifyBot") -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Start Verification",
        style=discord.ButtonStyle.primary,
        custom_id="fan_verify:start",
    )
    async def start_verification(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.bot.begin_verification(interaction)


class ReviewView(discord.ui.View):
    def __init__(self, bot: "FanVerifyBot", request_id: str) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.request_id = request_id

        approve = discord.ui.Button(
            label="Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"fan_verify:approve:{request_id}",
        )
        reject = discord.ui.Button(
            label="Reject",
            style=discord.ButtonStyle.danger,
            custom_id=f"fan_verify:reject:{request_id}",
        )
        approve.callback = self._approve
        reject.callback = self._reject
        self.add_item(approve)
        self.add_item(reject)

    async def _approve(self, interaction: discord.Interaction) -> None:
        await self.bot.handle_review(interaction=interaction, request_id=self.request_id, approved=True, view=self)

    async def _reject(self, interaction: discord.Interaction) -> None:
        await self.bot.handle_review(interaction=interaction, request_id=self.request_id, approved=False, view=self)


class FanInfoModal(discord.ui.Modal, title="Fan Verification"):
    tiktok_handle = discord.ui.TextInput(
        label="TikTok Handle",
        placeholder="@yourhandle",
        required=True,
        max_length=64,
    )
    email = discord.ui.TextInput(
        label="Email",
        placeholder="you@example.com",
        required=True,
        max_length=120,
    )
    phone = discord.ui.TextInput(
        label="Phone",
        placeholder="+1 555 123 4567",
        required=True,
        max_length=32,
    )
    date_of_birth = discord.ui.TextInput(
        label="Date of Birth",
        placeholder="YYYY-MM-DD",
        required=True,
        min_length=10,
        max_length=10,
    )
    favorite_member = discord.ui.TextInput(
        label="Favorite Wildcardz Member",
        placeholder="Type a member name",
        required=True,
        max_length=80,
    )

    def __init__(self, bot: "FanVerifyBot") -> None:
        super().__init__(timeout=600)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        guild = interaction.guild
        if guild is None or member is None:
            await interaction.response.send_message(
                "This verification flow only works inside a server.",
                ephemeral=True,
            )
            return

        email = str(self.email).strip()
        if not _is_valid_email(email):
            await interaction.response.send_message(
                "Please submit a valid email address.",
                ephemeral=True,
            )
            return
        dob_text = str(self.date_of_birth).strip()
        try:
            dob_date = datetime.strptime(dob_text, "%Y-%m-%d").date()
            if dob_date > datetime.now(timezone.utc).date():
                raise ValueError("future date")
        except Exception:
            await interaction.response.send_message(
                "Please enter date of birth as YYYY-MM-DD.",
                ephemeral=True,
            )
            return

        submission = PendingSubmission(
            request_id=self.bot.next_request_id(),
            guild_id=guild.id,
            user_id=member.id,
            discord_username=str(member),
            tiktok_handle=str(self.tiktok_handle).strip(),
            email=email,
            phone=str(self.phone).strip(),
            date_of_birth=dob_text,
            favorite_wildcardz_member=str(self.favorite_member).strip(),
            submitted_at=datetime.now(timezone.utc).isoformat(),
            donation_target_handle=self.bot.donation_target_handle,
        )
        total_donated_diamonds, donation_lookup_error = await self.bot.supabase.get_total_donations_to_target(
            donor_handle=submission.tiktok_handle,
            target_tiktok_handle=self.bot.donation_target_handle,
        )
        submission.total_donated_diamonds = total_donated_diamonds
        submission.donation_lookup_error = donation_lookup_error
        for existing_id, existing in list(self.bot.pending.items()):
            if existing.guild_id == guild.id and existing.user_id == member.id:
                self.bot.pending.pop(existing_id, None)
        self.bot.pending[submission.request_id] = submission

        admin_channel = self.bot.resolve_admin_channel(guild)
        if admin_channel is None:
            self.bot.pending.pop(submission.request_id, None)
            await interaction.response.send_message(
                "I could not find `#admin`. Ask a moderator to create it or set DISCORD_ADMIN_CHANNEL.",
                ephemeral=True,
            )
            return

        review_view = ReviewView(self.bot, submission.request_id)
        try:
            await admin_channel.send(embed=submission.to_embed(status="Pending"), view=review_view)
        except Exception:
            self.bot.pending.pop(submission.request_id, None)
            logger.exception("Failed to post verification preview to #admin")
            await interaction.response.send_message(
                "I could not post your preview to #admin. Please notify a moderator.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Your verification form was submitted to moderators for review.",
            ephemeral=True,
        )


class FanVerifyBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

        self.verify_channel_name = _env("DISCORD_VERIFY_CHANNEL", "verify").strip() or "verify"
        self.verify_channel_id = _env_int("DISCORD_VERIFY_CHANNEL_ID")
        self.admin_channel_name = _env("DISCORD_ADMIN_CHANNEL", "admin").strip() or "admin"
        self.admin_channel_id = _env_int("DISCORD_ADMIN_CHANNEL_ID")
        self.general_channel_name = _env("DISCORD_GENERAL_CHANNEL", "general").strip() or "general"
        self.general_channel_id = _env_int("DISCORD_GENERAL_CHANNEL_ID")
        self.verified_role_name = _env("DISCORD_VERIFIED_ROLE", "verified").strip() or "verified"
        self.verified_role_id = _env_int("DISCORD_VERIFIED_ROLE_ID")
        self.mod_role_name = _env("DISCORD_MOD_ROLE", "").strip()
        self.mod_role_id = _env_int("DISCORD_MOD_ROLE_ID")
        self.guild_id = _env_int("DISCORD_GUILD_ID")
        self.donation_target_handle = _normalize_handle(
            _env("DISCORD_VERIFY_DONATION_TARGET", "wildcard_boys").strip() or "wildcard_boys"
        )
        self.live_announce_handles = _parse_handles_csv(
            _env("DISCORD_LIVE_ANNOUNCE_HANDLES", "wildcard_boys,cardin_v_,zerokomodo")
        )
        self.live_poll_seconds = max(15, _env_int("DISCORD_LIVE_POLL_SECONDS", 60))
        self.live_announce_cooldown_seconds = 4 * 60 * 60

        self.pending: dict[str, PendingSubmission] = {}
        self.supabase = SupabaseFanInfoStore(table_name="fan_info")
        self._synced_commands = False
        self._live_clients: dict[str, TikTokLiveClient] = {}
        self._live_states: dict[str, bool] = {h: False for h in self.live_announce_handles}
        self._last_live_announce_ts: dict[str, float] = {}
        self._live_announce_task: Optional[asyncio.Task] = None
        self._missing_general_channel_warned: set[int] = set()
        self._last_live_poll_error_log = datetime.min.replace(tzinfo=timezone.utc)
        self._configure_live_web_defaults()

    async def setup_hook(self) -> None:
        # Register persistent button views after the event loop starts.
        self.add_view(VerificationStartView(self))

    def _configure_live_web_defaults(self) -> None:
        try:
            WebDefaults.tiktok_webcast_url = "https://webcast.us.tiktok.com/webcast"
            sign_api_key = _env("EULERSTREAM_API_KEY", "").strip()
            sign_url = _env("EULERSTREAM_SIGN_URL", "").strip()
            if sign_api_key:
                try:
                    WebDefaults.tiktok_sign_api_key = sign_api_key
                except Exception:
                    pass
            if sign_url:
                try:
                    WebDefaults.tiktok_sign_url = sign_url
                except Exception:
                    pass
        except Exception:
            logger.debug("Unable to configure TikTok WebDefaults for live announcements.")

    async def on_ready(self) -> None:
        if not self._synced_commands:
            try:
                if self.guild_id > 0:
                    await self.tree.sync(guild=discord.Object(id=self.guild_id))
                    logger.info("Synced slash commands to guild %s", self.guild_id)
                else:
                    await self.tree.sync()
                    logger.info("Synced global slash commands")
            except Exception:
                logger.exception("Failed to sync slash commands")
            self._synced_commands = True
        logger.info("Discord verify bot connected as %s (%s)", self.user, getattr(self.user, "id", "unknown"))
        if self.live_announce_handles and self._live_announce_task is None:
            self._live_announce_task = asyncio.create_task(
                self._live_announce_loop(),
                name="discord-live-announce-loop",
            )
            logger.info(
                "Started live announce loop for handles=%s interval=%ss",
                ",".join(self.live_announce_handles),
                self.live_poll_seconds,
            )

    async def close(self) -> None:
        if self._live_announce_task is not None:
            self._live_announce_task.cancel()
            try:
                await self._live_announce_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Live announce loop exited with error during shutdown")
            self._live_announce_task = None
        for handle, client in list(self._live_clients.items()):
            try:
                await client.disconnect()
            except Exception:
                pass
            finally:
                self._live_clients.pop(handle, None)
        await self.supabase.close()
        await super().close()

    def next_request_id(self) -> str:
        return secrets.token_hex(6)

    def resolve_verify_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        if self.verify_channel_id > 0:
            channel = guild.get_channel(self.verify_channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel
        wanted = self.verify_channel_name.lower()
        for channel in guild.text_channels:
            if channel.name.lower() == wanted:
                return channel
        return None

    def resolve_admin_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        if self.admin_channel_id > 0:
            channel = guild.get_channel(self.admin_channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel
        wanted = self.admin_channel_name.lower()
        for channel in guild.text_channels:
            if channel.name.lower() == wanted:
                return channel
        return None

    def resolve_general_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        if self.general_channel_id > 0:
            channel = guild.get_channel(self.general_channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel
        wanted = self.general_channel_name.lower()
        for channel in guild.text_channels:
            if channel.name.lower() == wanted:
                return channel
        return None

    async def _is_handle_live(self, handle: str) -> bool:
        normalized = _normalize_handle(handle)
        if not normalized:
            return False
        client = self._live_clients.get(normalized)
        if client is None:
            client = TikTokLiveClient(unique_id=f"@{normalized}")
            self._live_clients[normalized] = client
        status = await client.is_live()
        return bool(status)

    async def _announce_handle_live(self, handle: str) -> None:
        normalized = _normalize_handle(handle)
        if not normalized:
            return
        now_ts = datetime.now(timezone.utc).timestamp()
        last_ts = float(self._last_live_announce_ts.get(normalized, 0.0))
        if last_ts > 0.0 and (now_ts - last_ts) < self.live_announce_cooldown_seconds:
            return
        live_url = f"https://www.tiktok.com/@{normalized}/live"
        message = f"@{normalized} is live now: {live_url}"

        target_guilds: list[discord.Guild] = []
        if self.guild_id > 0:
            guild = self.get_guild(self.guild_id)
            if guild is not None:
                target_guilds.append(guild)
        else:
            target_guilds.extend(self.guilds)

        for guild in target_guilds:
            channel = self.resolve_general_channel(guild)
            if channel is None:
                if guild.id not in self._missing_general_channel_warned:
                    self._missing_general_channel_warned.add(guild.id)
                    logger.warning(
                        "Could not find general channel '%s' in guild=%s for live announcement.",
                        self.general_channel_name,
                        guild.id,
                    )
                continue
            try:
                await channel.send(message)
            except Exception:
                logger.exception(
                    "Failed to send live announcement for @%s to guild=%s channel=%s",
                    normalized,
                    guild.id,
                    channel.id,
                )
        self._last_live_announce_ts[normalized] = now_ts

    async def _live_announce_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            for handle in self.live_announce_handles:
                normalized = _normalize_handle(handle)
                if not normalized:
                    continue
                try:
                    live_now = await self._is_handle_live(normalized)
                    was_live = self._live_states.get(normalized, False)
                    if live_now and not was_live:
                        await self._announce_handle_live(normalized)
                    self._live_states[normalized] = live_now
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    now = datetime.now(timezone.utc)
                    if (now - self._last_live_poll_error_log).total_seconds() >= 60:
                        self._last_live_poll_error_log = now
                        logger.warning(
                            "Live status check failed for @%s: %s: %s",
                            normalized,
                            type(exc).__name__,
                            exc,
                        )
                    # Recreate client on next poll if this one got into a bad state.
                    self._live_clients.pop(normalized, None)
            await asyncio.sleep(self.live_poll_seconds)

    def resolve_verified_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        if self.verified_role_id > 0:
            role = guild.get_role(self.verified_role_id)
            if role is not None:
                return role
        wanted = self.verified_role_name.lower()
        for role in guild.roles:
            if role.name.lower() == wanted:
                return role
        return None

    def resolve_mod_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        if self.mod_role_id > 0:
            role = guild.get_role(self.mod_role_id)
            if role is not None:
                return role
        if not self.mod_role_name:
            return None
        wanted = self.mod_role_name.lower()
        for role in guild.roles:
            if role.name.lower() == wanted:
                return role
        return None

    def is_verified_member(self, member: discord.Member) -> bool:
        role = self.resolve_verified_role(member.guild)
        return role in member.roles if role is not None else False

    def can_review(self, member: discord.Member) -> bool:
        perms = member.guild_permissions
        if perms.administrator or perms.manage_guild or perms.manage_roles:
            return True
        mod_role = self.resolve_mod_role(member.guild)
        if mod_role is not None and mod_role in member.roles:
            return True
        return False

    async def begin_verification(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        guild = interaction.guild
        if guild is None or member is None:
            await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
            return

        verify_channel = self.resolve_verify_channel(guild)
        if verify_channel is not None and interaction.channel_id != verify_channel.id:
            await interaction.response.send_message(
                f"Please complete verification in {verify_channel.mention}.",
                ephemeral=True,
            )
            return

        if self.is_verified_member(member):
            await interaction.response.send_message("You are already verified.", ephemeral=True)
            return

        await interaction.response.send_modal(FanInfoModal(self))

    async def handle_review(
        self,
        interaction: discord.Interaction,
        request_id: str,
        approved: bool,
        view: ReviewView,
    ) -> None:
        reviewer = interaction.user if isinstance(interaction.user, discord.Member) else None
        guild = interaction.guild
        if reviewer is None or guild is None:
            await interaction.response.send_message("This action can only run inside a server.", ephemeral=True)
            return
        if not self.can_review(reviewer):
            await interaction.response.send_message("Only moderators can approve or reject submissions.", ephemeral=True)
            return

        submission = self.pending.get(request_id)
        if submission is None:
            await interaction.response.send_message("This request was already processed or expired.", ephemeral=True)
            return
        if submission.guild_id != guild.id:
            await interaction.response.send_message("This request belongs to a different server.", ephemeral=True)
            return

        if approved:
            ok, error = await self.supabase.insert_fan_info(submission.to_supabase_row())
            if not ok:
                await interaction.response.send_message(f"Approval failed: {error}", ephemeral=True)
                return

        role_assignment_ok = True
        role_assignment_error = ""

        self.pending.pop(request_id, None)
        for item in view.children:
            item.disabled = True

        target_member: Optional[discord.Member] = guild.get_member(submission.user_id)
        if approved and target_member is not None:
            verified_role = self.resolve_verified_role(guild)
            if verified_role is not None and verified_role not in target_member.roles:
                try:
                    await target_member.add_roles(verified_role, reason=f"Verification approved by {reviewer}")
                except Exception as exc:
                    role_assignment_ok = False
                    role_assignment_error = f"{type(exc).__name__}: {exc}"
                    logger.exception("Failed to add verified role to user_id=%s", target_member.id)
                    # Check whether role assignment still succeeded despite response decoding issues.
                    try:
                        refreshed = await guild.fetch_member(target_member.id)
                        if verified_role in refreshed.roles:
                            role_assignment_ok = True
                            role_assignment_error = ""
                    except Exception:
                        pass

        reviewed_by = f"{reviewer} ({reviewer.id})"
        if approved and not role_assignment_ok:
            status = "Approved (Role Pending)"
            reason = (
                "Saved to Supabase fan_info, but failed to add verified role. "
                f"Error: {role_assignment_error}"
            )
        elif approved:
            status = "Approved"
            reason = "Saved to Supabase fan_info."
        else:
            status = "Rejected"
            reason = "Moderator rejected submission."
        embed = submission.to_embed(status=status, reviewed_by=reviewed_by, reason=reason)

        await interaction.response.edit_message(embed=embed, view=view)

        verify_channel = self.resolve_verify_channel(guild)
        if verify_channel is not None:
            try:
                if approved:
                    if role_assignment_ok:
                        await verify_channel.send(
                            f"<@{submission.user_id}> your verification was approved. Welcome!"
                        )
                    else:
                        await verify_channel.send(
                            f"<@{submission.user_id}> your info was approved, but there was an issue assigning your verified role. "
                            "A moderator will finish verification shortly."
                        )
                else:
                    await verify_channel.send(
                        f"<@{submission.user_id}> your verification was rejected. Please resubmit in {verify_channel.mention}."
                    )
            except Exception:
                logger.exception("Failed to send verification result message for user_id=%s", submission.user_id)

        if approved and not role_assignment_ok:
            await interaction.followup.send(
                "Unfortunately I could not assign the verified role."
                "Please check bot role hierarchy/permissions and retry role assignment manually.",
                ephemeral=True,
            )

    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        if self.is_verified_member(member):
            return
        verify_channel = self.resolve_verify_channel(member.guild)
        if verify_channel is None:
            logger.warning(
                "Could not find verify channel '%s' in guild=%s",
                self.verify_channel_name,
                member.guild.id,
            )
            return
        try:
            await verify_channel.send(
                f"{member.mention} welcome. Please click the button below to verify.",
                view=VerificationStartView(self),
            )
        except Exception:
            logger.exception("Failed to send verify prompt for user_id=%s", member.id)


def create_bot() -> FanVerifyBot:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    if _env_bool("DISCORD_SUPPRESS_RECONNECT_TRACEBACKS", True):
        _install_discord_reconnect_noise_filter()
    bot = FanVerifyBot()
    if bot.guild_id > 0:
        @bot.tree.command(
            name="verify",
            description="Submit your fan verification form",
            guild=discord.Object(id=bot.guild_id),
        )
        async def verify_slash(interaction: discord.Interaction) -> None:
            await bot.begin_verification(interaction)
    else:
        @bot.tree.command(name="verify", description="Submit your fan verification form")
        async def verify_slash(interaction: discord.Interaction) -> None:
            await bot.begin_verification(interaction)
    return bot


def main() -> None:
    token = _env("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Missing DISCORD_BOT_TOKEN. Add it to app.env or environment.")
    bot = create_bot()
    bot.run(token)


if __name__ == "__main__":
    main()
