"""
Notification Service for sending updates via Telegram
"""
import aiohttp
import os
from typing import Optional
import logging

logger = logging.getLogger(__name__)

class NotificationService:
    def __init__(self):
        self.bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.enabled = bool(self.bot_token and self.chat_id)
        
        if not self.enabled:
            logger.warning("Telegram notifications disabled - missing bot token or chat ID")
    
    async def send_message(self, message: str):
        """Send message via Telegram"""
        if not self.enabled:
            logger.info(f"Notification (disabled): {message}")
            return
        
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        data = {
            'chat_id': self.chat_id,
            'text': message,
            'parse_mode': 'HTML'
        }
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=data) as response:
                    if response.status == 200:
                        logger.info("Notification sent successfully")
                    else:
                        logger.error(f"Failed to send notification: {response.status}")
            except Exception as e:
                logger.error(f"Telegram notification failed: {str(e)}")
    
    async def send_notification(self, message: str):
        """Send notification (alias for send_message)"""
        await self.send_message(message)
    
    async def send_error(self, error_message: str):
        """Send error notification"""
        message = f"🚨 <b>Trakt Sync Error</b>\n\n{error_message}"
        await self.send_message(message)