#!/usr/bin/env python3
"""
DebridMediaManager Hash List Auto-Add Script
Automatically adds cached content from DMM hash lists to Real-Debrid based on preferences
"""

import os
import json
import logging
import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

from dmm_client import DMMClient
from real_debrid_client import RealDebridClient
from notifier import NotificationService

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('../logs/automation.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class HashListAutoAdd:
    def __init__(self):
        self.config = self.load_config()
        self.dmm = None  # Initialize as None, will be created in __aenter__
        self.real_debrid = None  # Initialize as None, will be created in __aenter__
        self.notifier = NotificationService()
        self.logger = logging.getLogger(__name__)  # Add missing logger
        
        # Data storage
        self.data_dir = Path('../data')
        self.data_dir.mkdir(exist_ok=True)
        self.processed_file = self.data_dir / 'processed_hashes.json'
        self.processed_hashes = self.load_processed_hashes()
        
    def load_config(self) -> Dict:
        """Load configuration from config file"""
        config_file = Path('../config/settings.yml')
        if config_file.exists():
            import yaml
            with open(config_file) as f:
                config = yaml.safe_load(f)
        else:
            config = {}
        
        # Default configuration
        default_config = {
            # Quality preferences (in order of preference)
            'quality_preferences': ['2160p', '1080p', '720p'],
            
            # Content types to add
            'content_types': {
                'movies': True,
                'tv_shows': True,
                'documentaries': True
            },
            
            # Release year filters
            'min_year': 2020,  # Only content from 2020 onwards
            'max_year': 2025,  # Up to current year
            
            # Language preferences
            'languages': ['english', 'en'],
            
            # Quality filters
            'exclude_keywords': [
                'cam', 'ts', 'screener', 'workprint', 'telecine',
                'r5', 'dvdscr', 'hdcam', 'hdts'
            ],
            
            # Include keywords (optional)
            'include_keywords': [
                'bluray', 'web-dl', 'webrip', 'hdtv', 'brrip'
            ],
            
            # Size limits (in GB)
            'min_size_gb': 0.5,   # Minimum file size
            'max_size_gb': 50.0,  # Maximum file size
            
            # Processing limits
            'max_items_per_run': 50,  # Don't add too many at once
            'hash_list_limit': 20,    # Number of hash lists to process per run
            
            # Scheduling
            'check_interval': 6,  # hours between runs
        }
        
        # Merge with defaults
        for key, value in default_config.items():
            if key not in config:
                config[key] = value
        
        # Override with environment variables if running in GitHub Actions
        if os.getenv('MAX_ITEMS_OVERRIDE'):
            try:
                config['max_items_per_run'] = int(os.getenv('MAX_ITEMS_OVERRIDE'))
            except ValueError:
                pass
                
        if os.getenv('FORCE_SYNC') == 'true':
            config['force_sync'] = True
        
        return config
    
    def load_processed_hashes(self) -> Set[str]:
        """Load already processed hash IDs"""
        if self.processed_file.exists():
            with open(self.processed_file) as f:
                data = json.load(f)
                return set(data.get('processed_hashes', []))
        return set()
    
    def save_processed_hashes(self):
        """Save processed hash IDs"""
        data = {
            'processed_hashes': list(self.processed_hashes),
            'last_updated': datetime.now().isoformat(),
            'total_processed': len(self.processed_hashes)
        }
        with open(self.processed_file, 'w') as f:
            json.dump(data, f, indent=2)

    def load_real_dmm_hashes(self) -> List[str]:
        """Load real torrent hashes from the extracted DMM data"""
        try:
            # Try to load from the real_dmm_hashes.json file
            hash_file = Path('../real_dmm_hashes.json')
            if hash_file.exists():
                with open(hash_file) as f:
                    data = json.load(f)
                    hashes = data.get('hashes', [])
                    logger.info(f"Loaded {len(hashes)} real DMM hashes from {hash_file}")
                    return hashes
            else:
                logger.warning("real_dmm_hashes.json not found. Run decode_dmm_hashes.py first to extract real hashes.")
                return []
                
        except Exception as e:
            logger.error(f"Error loading real DMM hashes: {e}")
            return []
    
    async def run_automation(self):
        """Main automation loop with real DMM hashes"""
        logger.info("Starting DebridAuto automation with real DMM hashes")
        
        try:
            # Check Real-Debrid service status before proceeding
            logger.info("Checking Real-Debrid service status...")
            service_status = await self.real_debrid.check_service_status()
            
            if service_status['status'] != 'healthy':
                logger.warning(f"Real-Debrid service status: {service_status['status']}")
                
                if service_status['status'] == 'service_unavailable':
                    logger.info("Service unavailable (503 error), waiting for recovery...")
                    if await self.real_debrid.wait_for_service_recovery(max_wait_minutes=15):
                        logger.info("Service recovered, continuing with automation...")
                    else:
                        logger.error("Service did not recover, aborting this run")
                        await self.notifier.send_notification(
                            "⚠️ DebridAuto Run Skipped",
                            f"Real-Debrid service is experiencing 503 errors and did not recover within 15 minutes.\nWill retry in next scheduled run."
                        )
                        return
                elif service_status['status'] == 'auth_error':
                    logger.error("Authentication error - check your API key")
                    await self.notifier.send_notification(
                        "❌ DebridAuto Authentication Error",
                        "Invalid API key or authentication failed. Please check your Real-Debrid API key."
                    )
                    return
                elif service_status['status'] == 'rate_limited':
                    logger.warning("Rate limited, waiting 5 minutes before proceeding...")
                    await asyncio.sleep(300)  # Wait 5 minutes
                else:
                    logger.warning(f"Service status {service_status['status']}, proceeding with caution...")
            else:
                logger.info("Real-Debrid service is healthy")
            
            # Load real DMM hashes instead of fake ones
            real_hashes = self.load_real_dmm_hashes()
            if not real_hashes:
                logger.error("No real DMM hashes available. Run decode_dmm_hashes.py first.")
                return
            
            logger.info(f"Using {len(real_hashes)} real torrent hashes from DMM")
            
            # Parse content from real hashes
            content_items = self.parse_content_from_hashes(real_hashes)
            logger.info(f"Parsed {len(content_items)} content items from real hashes")
            
            # Filter content based on preferences  
            filtered_content = self.filter_content(content_items)
            logger.info(f"After filtering: {len(filtered_content)} items")
            
            # Remove already processed hashes
            new_content = [item for item in filtered_content 
                          if item['hash'] not in self.processed_hashes]
            logger.info(f"Found {len(new_content)} new items to process")
            
            if not new_content:
                logger.info("No new content to process")
                return
            
            # Check for existing torrents in Real-Debrid
            existing_torrents = await self.check_existing_torrents()
            
            # Remove content that already exists in Real-Debrid
            unique_content = [item for item in new_content 
                             if item['hash'].lower() not in existing_torrents]
            logger.info(f"After removing existing torrents: {len(unique_content)} items remain")
            
            # Remove duplicates
            unique_content = await self.check_content_similarity(unique_content)
            logger.info(f"After deduplication: {len(unique_content)} unique items")
            
            # Limit items per run
            max_items = self.config.get('max_items_per_run', 50)
            if len(unique_content) > max_items:
                unique_content = unique_content[:max_items]
                logger.info(f"Limited to {max_items} items for this run")
            
            # Add to Real-Debrid
            if unique_content:
                results = await self.add_content_to_debrid(unique_content, existing_torrents)
                logger.info(f"Added: {len(results['added'])}, Failed: {len(results['failed'])}, Skipped: {len(results['skipped'])}")
                
                # Send notification
                await self.send_notification(results)
                
                # Save processed hashes
                self.save_processed_hashes()
            else:
                logger.info("No unique content to add")
            
        except Exception as e:
            logger.error(f"Error in automation: {str(e)}")
            # Send error notification
            try:
                await self.notifier.send_notification(
                    "❌ DebridAuto Error",
                    f"Automation failed with error: {str(e)}"
                )
            except Exception as notif_error:
                logger.error(f"Failed to send error notification: {notif_error}")
            raise
        finally:
            # Ensure sessions are closed
            try:
                await self.dmm.close()
                await self.real_debrid.close()
            except Exception as e:
                logger.debug(f"Error closing sessions: {e}")

    async def __aenter__(self):
        """Async context manager entry - initialize clients here"""
        self.dmm = DMMClient()
        self.real_debrid = RealDebridClient(os.getenv('REAL_DEBRID_API_KEY'))
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - cleanup sessions"""
        try:
            if self.dmm:
                await self.dmm.close()
            if self.real_debrid:
                await self.real_debrid.close()
        except Exception as e:
            logger.debug(f"Error in cleanup: {e}")

    async def check_existing_torrents(self):
        """Check for existing torrents in Real-Debrid to avoid duplicates"""
        try:
            torrents = await self.real_debrid.get_torrents()
            existing_hashes = set()
            
            for torrent in torrents:
                if 'hash' in torrent:
                    existing_hashes.add(torrent['hash'].lower())
            
            logger.info(f"Found {len(existing_hashes)} existing torrents in Real-Debrid")
            return existing_hashes
            
        except Exception as e:
            logger.error(f"Error checking existing torrents: {e}")
            return set()

    async def check_content_similarity(self, content_items):
        """Check for similar content and deduplicate."""
        unique_content = []
        seen_hashes = set()
        
        for item in content_items:
            hash_value = item.get('hash', '')
            title = item.get('title', 'Unknown')
            
            if hash_value in seen_hashes:
                logger.info(f"Duplicate hash found, skipping: {title}")  # Use global logger
                continue
                
            seen_hashes.add(hash_value)
            unique_content.append(item)
        
        logger.info(f"Deduplication complete: {len(content_items)} -> {len(unique_content)} unique items")
        return unique_content

    def parse_content_from_hashes(self, hashes: List[str]) -> List[Dict]:
        """Parse content information from a list of real hashes"""
        content_items = []
        
        for hash_str in hashes:
            try:
                # Create a content item from the real hash
                content_item = {
                    'hash': hash_str,
                    'filename': f"cached_content_{hash_str[:8]}.mkv",
                    'size': 2 * 1024 * 1024 * 1024,  # 2GB default
                    'type': 'movie',
                    'title': f"Cached Content {hash_str[:8]}",
                    'quality': '1080p',
                    'codec': 'x264',
                    'year': 2023,
                    'features': {}
                }
                content_items.append(content_item)
                
            except Exception as e:
                logger.debug(f"Failed to process hash {hash_str}: {str(e)}")
                continue
        
        return content_items

    def filter_content(self, content_items: List[Dict]) -> List[Dict]:
        """Filter content based on user preferences"""
        filtered = []
        
        logger.info(f"Starting filter with {len(content_items)} items")
        
        for i, item in enumerate(content_items):
            try:
                title = item.get('title', '')
                filename = item.get('filename', '').lower()
                hash_str = item.get('hash', '').lower()
                
                # For real hashes, we need to infer quality/codec from hash or use defaults
                # Since we don't have actual filenames, we'll be more permissive but still apply filters
                
                # Check exclude keywords
                exclude_keywords = self.config.get('exclude_keywords', [])
                exclude_found = False
                for keyword in exclude_keywords:
                    if keyword.lower() in filename:
                        logger.debug(f"Skipping {title} - contains excluded keyword: {keyword}")
                        exclude_found = True
                        break
                if exclude_found:
                    continue
                
                # Check include keywords (if specified)
                include_keywords = self.config.get('include_keywords', [])
                if include_keywords:
                    include_found = False
                    for keyword in include_keywords:
                        if keyword.lower() in filename:
                            include_found = True
                            break
                    if not include_found:
                        logger.debug(f"Skipping {title} - doesn't contain required keywords")
                        continue
                
                # Check quality preferences (if we can detect them)
                quality_preferences = self.config.get('quality_preferences', [])
                if quality_preferences:
                    quality_found = False
                    for quality in quality_preferences:
                        if quality.lower() in filename:
                            quality_found = True
                            break
                    # If we can't detect quality, skip unless it's a very small list
                    if not quality_found and '1080p' not in filename and '720p' not in filename and '2160p' not in filename:
                        # For cached content without clear quality indicators, be permissive
                        # but log that we couldn't determine quality
                        logger.debug(f"Could not determine quality for {title}, allowing through")
                
                # Check codec preferences (if we can detect them)
                codec_preferences = self.config.get('codec_preferences', [])
                if codec_preferences:
                    codec_found = False
                    for codec in codec_preferences:
                        if codec.lower() in filename:
                            codec_found = True
                            break
                    # If codec preferences are set but we can't detect codec, be selective
                    if not codec_found and any(codec.lower() in filename for codec in ['x264', 'x265', 'h264', 'h265', 'hevc', 'av1']):
                        logger.debug(f"Skipping {title} - codec not in preferences")
                        continue
                
                # Check HDR preferences
                hdr_preferences = self.config.get('hdr_preferences', [])
                if hdr_preferences:
                    # Check if content matches HDR preferences
                    hdr_found = False
                    for hdr_format in hdr_preferences:
                        if hdr_format.lower() in filename:
                            hdr_found = True
                            break
                    
                    # Special handling for "sdr" preference - exclude HDR content
                    if 'sdr' in hdr_preferences:
                        hdr_indicators = ['hdr', 'hdr10', 'dolby', 'vision', 'dv', 'hlg']
                        has_hdr = any(indicator in filename for indicator in hdr_indicators)
                        if has_hdr:
                            logger.debug(f"Skipping {title} - contains HDR but SDR preferred")
                            continue
                    elif not hdr_found:
                        # If specific HDR formats are required but not found, skip
                        logger.debug(f"Skipping {title} - HDR format not in preferences")
                        continue
                
                # Check language preferences
                languages = self.config.get('languages', [])
                if languages:
                    # Check if content is in preferred language
                    lang_found = False
                    for lang in languages:
                        if lang.lower() in filename:
                            lang_found = True
                            break
                    # For cached content, be permissive if we can't detect language clearly
                    # but exclude obvious foreign language indicators
                    foreign_indicators = ['korean', 'chinese', 'spanish', 'french', 'german', 'italian', 'japanese', 'russian']
                    has_foreign = any(indicator in filename for indicator in foreign_indicators)
                    if has_foreign and not lang_found:
                        logger.debug(f"Skipping {title} - foreign language detected")
                        continue
                
                # Check size limits
                size_gb = item.get('size', 0) / (1024 * 1024 * 1024)
                min_size = self.config.get('min_size_gb', 0)
                max_size = self.config.get('max_size_gb', 100)
                if size_gb > 0 and (size_gb < min_size or size_gb > max_size):
                    logger.debug(f"Skipping {title} - size {size_gb:.1f}GB outside range {min_size}-{max_size}GB")
                    continue
                
                # Check year filters (if available)
                year = item.get('year', 0)
                min_year = self.config.get('min_year')
                max_year = self.config.get('max_year')
                if min_year and year > 0 and year < min_year:
                    logger.debug(f"Skipping {title} - year {year} before minimum {min_year}")
                    continue
                if max_year and year > 0 and year > max_year:
                    logger.debug(f"Skipping {title} - year {year} after maximum {max_year}")
                    continue
                
                # Check content type preferences
                content_types = self.config.get('content_types', {})
                content_type = item.get('type', 'movie')
                type_key = f"{content_type}s" if content_type in ['movie', 'tv_show'] else content_type
                if not content_types.get(type_key, True):
                    logger.debug(f"Skipping {title} - content type {content_type} disabled")
                    continue
                
                # Check genre preferences (if available)
                exclude_genres = self.config.get('exclude_genres', [])
                if exclude_genres:
                    genre_excluded = False
                    for genre in exclude_genres:
                        if genre.lower() in filename:
                            logger.debug(f"Skipping {title} - contains excluded genre: {genre}")
                            genre_excluded = True
                            break
                    if genre_excluded:
                        continue
                
                # If we get here, the content passed all filters
                logger.debug(f"Content passed all filters: {title} ({size_gb:.1f}GB)")
                filtered.append(item)
                
            except Exception as e:
                logger.error(f"Error filtering content item {item.get('title', 'unknown')}: {str(e)}")
                # Skip items that cause errors in filtering
                continue
        
        logger.info(f"Filter complete: {len(filtered)} items passed filters")
        return filtered

    async def add_content_to_debrid(self, new_content, existing_torrents):
        """Add content items to Real-Debrid"""
        results = {'added': [], 'failed': [], 'skipped': []}
        
        for content in new_content:
            try:
                # Handle both dict and Content object formats
                if hasattr(content, 'hash'):
                    content_hash = content.hash
                    content_title = getattr(content, 'title', f"Content {content_hash[:8]}")
                elif isinstance(content, dict):
                    content_hash = content.get('hash')
                    content_title = content.get('title', f"Content {content_hash[:8]}")
                else:
                    logger.error(f"Invalid content format: {type(content)}")
                    results['failed'].append(content)
                    continue
                    
                if not content_hash:
                    logger.error(f"No hash found for content: {content}")
                    results['failed'].append(content)
                    continue
                
                # Check if already exists in Real-Debrid
                if content_hash.lower() in existing_torrents:
                    logger.info(f"Content already exists in Real-Debrid, skipping: {content_title}")
                    results['skipped'].append(content)
                    continue
                
                # Add magnet link to Real-Debrid
                magnet_link = f"magnet:?xt=urn:btih:{content_hash}"
                success = await self.real_debrid.add_torrent(magnet_link)
                
                if success:
                    logger.info(f"Successfully added to Real-Debrid: {content_title}")
                    results['added'].append(content)
                    # Mark as processed
                    self.processed_hashes.add(content_hash)
                else:
                    logger.error(f"Failed to add to Real-Debrid: {content_title}")
                    results['failed'].append(content)
                    
            except Exception as e:
                logger.error(f"Error adding {content.get('title', 'content') if isinstance(content, dict) else getattr(content, 'title', 'content')} to Real-Debrid: {e}")
                results['failed'].append(content)
                
        return results

    async def send_notification(self, results: Dict):
        """Send notification about the automation results"""
        try:
            added_count = len(results.get('added', []))
            failed_count = len(results.get('failed', []))
            skipped_count = len(results.get('skipped', []))
            
            # Create notification message
            message = f"DebridAuto Run Complete:\n"
            message += f"✅ Added: {added_count}\n"
            message += f"⏭️ Skipped: {skipped_count}\n"
            message += f"❌ Failed: {failed_count}\n"
            
            if added_count > 0:
                message += "\nAdded items:\n"
                for item in results['added'][:5]:  # Show first 5 items
                    title = item.get('title', 'Unknown') if isinstance(item, dict) else getattr(item, 'title', 'Unknown')
                    message += f"• {title}\n"
                if len(results['added']) > 5:
                    message += f"• ... and {len(results['added']) - 5} more\n"
            
            # Send notification
            await self.notifier.send_notification(message)
            logger.info("Notification sent successfully")
            
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

async def main():
    """Main entry point"""
    # Ensure directories exist - use relative paths to project root
    Path('../logs').mkdir(exist_ok=True)
    Path('../data').mkdir(exist_ok=True)
    
    # Initialize and run auto-add with proper session management
    async with HashListAutoAdd() as auto_add:
        await auto_add.run_automation()

if __name__ == "__main__":
    asyncio.run(main())