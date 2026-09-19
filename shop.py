from __future__ import annotations

import logging
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("craftopia-bot.shop")

API_BASE = os.getenv(
    "CRAFTOPIA_WALLET_API_BASE",
    "https://store.craftopics.online/api/discord",
).rstrip("/")
API_KEY = os.getenv("CRAFTOPIA_BOT_API_KEY", "").strip()


async def api(method: str, path: str, *, payload: dict | None = None) -> tuple[int, dict]:
    if not API_KEY:
        return 503, {"error": "Bot chưa được cấu hình CRAFTOPIA_BOT_API_KEY."}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-craftopia-bot-key": API_KEY,
    }
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method.upper(),
                f"{API_BASE}/{path.lstrip('/')}",
                headers=headers,
                json=payload if payload is not None else None,
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    data = {"error": "API trả dữ liệu không hợp lệ."}
                return response.status, data if isinstance(data, dict) else {"data": data}
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        logger.warning("Craftopia shop API request failed: %s", exc)
        return 503, {"error": "Không kết nối được Craftopia Wallet API."}


def private_delivery_text(data: dict) -> str:
    def clean(value: object) -> str:
        return str(value or "").replace("`", "'").replace("\r", " ").replace("\n", " ").strip()

    gmail = clean(data.get("gmail"))
    password = clean(data.get("password"))
    two_factor = clean(data.get("twoFactor"))
    product_name = clean(data.get("productName") or data.get("productKey") or "Sản phẩm")
    order_key = clean(data.get("orderKey"))
    return (
        f"✅ **Đơn hàng {product_name} đã hoàn tất**\n"
        f"-# Mã đơn: `{order_key}`\n\n"
        "```text\n"
        f"Gmail: {gmail}\n"
        f"Password: {password}\n"
        f"2FA: {two_factor}\n"
        "```\n"
        "⚠️ Đây là thông tin riêng tư. Không chia sẻ message này cho người khác."
    )


class ProductPurchaseView(discord.ui.View):
    def __init__(self, requester_id: int, product: dict, order_key: str) -> None:
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.product = product
        self.order_key = order_key
        self.finished = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Nút mua hàng này không dành cho bạn.", ephemeral=True)
            return False
        return True

    def disable_all(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    @discord.ui.button(label="Xác nhận mua", style=discord.ButtonStyle.success, emoji="🛒")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.finished:
            await interaction.response.send_message("Đơn này đã được xử lý.", ephemeral=True)
            return

        self.finished = True
        self.disable_all()
        await interaction.response.edit_message(view=self)

        status, data = await api(
            "POST",
            "purchase",
            payload={
                "discordId": str(interaction.user.id),
                "productKey": str(self.product.get("key") or ""),
                "orderKey": self.order_key,
            },
        )

        if status == 200 and data.get("ok"):
            delivery = private_delivery_text(data)
            try:
                await interaction.user.send(delivery)
                sent_dm = True
            except (discord.Forbidden, discord.HTTPException):
                sent_dm = False

            balance = int(data.get("balance", 0) or 0)
            price = int(data.get("priceCoin", self.product.get("priceCoin", 0)) or 0)
            if sent_dm:
                await interaction.followup.send(
                    f"✅ Thanh toán **{price:,} Coin** thành công.\n"
                    f"Số dư còn lại: **{balance:,} Coin**.\n"
                    "📩 Tài khoản đã được gửi vào **DM riêng** của bạn.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"✅ Thanh toán **{price:,} Coin** thành công.\n"
                    f"Số dư còn lại: **{balance:,} Coin**.\n"
                    "Discord đang chặn DM nên thông tin được gửi riêng tại đây:\n\n" + delivery,
                    ephemeral=True,
                )
            self.stop()
            return

        error = str(data.get("error") or "Không thể mua sản phẩm.")
        if data.get("notLinked"):
            error += "\nHãy liên kết Craftopia Wallet bằng `/link` trước."
        elif data.get("insufficientBalance"):
            error += "\nHãy nạp thêm Coin trên `store.craftopics.online`."
        elif data.get("outOfStock"):
            error += "\nSản phẩm hiện đã hết stock."
        await interaction.followup.send(f"❌ {error}", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Hủy", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.finished:
            await interaction.response.send_message("Đơn này đã được xử lý.", ephemeral=True)
            return
        self.finished = True
        self.disable_all()
        await interaction.response.edit_message(content="Đã hủy giao dịch.", view=self)
        self.stop()


class ShopCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="shop", description="Xem sản phẩm đang bán bằng Craftopia Coin")
    @app_commands.checks.cooldown(5, 20.0, key=lambda interaction: (interaction.guild_id, interaction.user.id))
    async def shop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        status, data = await api("GET", "products")
        if status != 200:
            await interaction.followup.send(f"❌ {data.get('error', 'Không tải được cửa hàng.')}", ephemeral=True)
            return

        products = data.get("products") or []
        if not products:
            await interaction.followup.send("Kho hiện chưa có sản phẩm đang bán.", ephemeral=True)
            return

        lines = ["🛍️ **CRAFTOPIA SHOP**", ""]
        for product in products[:20]:
            key = str(product.get("key") or "")
            name = str(product.get("name") or key)
            price = int(product.get("priceCoin", 0) or 0)
            available = int(product.get("available", 0) or 0)
            stock_text = f"{available} còn hàng" if available > 0 else "HẾT HÀNG"
            lines += [f"**{name}** — `{key}`", f"💎 {price:,} Coin · 📦 {stock_text}", ""]
        lines.append("Dùng `/buy product:<product_key>` để mua.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="buy", description="Mua sản phẩm bằng Craftopia Coin")
    @app_commands.describe(product="Product key hiển thị trong /shop, ví dụ: capcut")
    @app_commands.checks.cooldown(3, 20.0, key=lambda interaction: (interaction.guild_id, interaction.user.id))
    async def buy(self, interaction: discord.Interaction, product: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        status, data = await api("GET", "products")
        if status != 200:
            await interaction.followup.send(f"❌ {data.get('error', 'Không tải được cửa hàng.')}", ephemeral=True)
            return

        wanted = product.strip().lower()
        selected = next(
            (p for p in (data.get("products") or []) if str(p.get("key") or "").lower() == wanted),
            None,
        )
        if not selected:
            await interaction.followup.send("❌ Không tìm thấy sản phẩm này. Dùng `/shop` để xem product key.", ephemeral=True)
            return

        available = int(selected.get("available", 0) or 0)
        price = int(selected.get("priceCoin", 0) or 0)
        if available <= 0:
            await interaction.followup.send("❌ Sản phẩm này hiện đã hết hàng.", ephemeral=True)
            return
        if price <= 0:
            await interaction.followup.send("❌ Sản phẩm chưa được cấu hình giá.", ephemeral=True)
            return

        view = ProductPurchaseView(interaction.user.id, selected, f"BUY_{interaction.id}")
        await interaction.followup.send(
            f"🛒 **Xác nhận mua {selected.get('name', wanted)}**\n"
            f"Giá: **{price:,} Coin** · Còn **{available}** stock\n\n"
            "Sau khi thanh toán, tài khoản sẽ được gửi **riêng qua DM**.",
            view=view,
            ephemeral=True,
        )
