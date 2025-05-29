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
            'max_items_per_run': 30,      # Don't add too many at once
            'hash_list_limit': 15,        # Number of hash lists to process per run
            'check_interval': 6,          # Hours between runs
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
        """Main automation loop that processes through all available DMM hash lists"""
        logger.info("Starting DebridAuto automation with DMM hash list processing")
        
        # Always ensure the processed hashes file exists, even if empty
        self.save_processed_hashes()
        
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
                        self.save_processed_hashes()
                        return
                elif service_status['status'] == 'auth_error':
                    logger.error("Authentication error - check your API key")
                    await self.notifier.send_notification(
                        "❌ DebridAuto Authentication Error",
                        "Invalid API key or authentication failed. Please check your Real-Debrid API key."
                    )
                    self.save_processed_hashes()
                    return
                elif service_status['status'] == 'rate_limited':
                    logger.warning("Rate limited, waiting 5 minutes before proceeding...")
                    await asyncio.sleep(300)  # Wait 5 minutes
                else:
                    logger.warning(f"Service status {service_status['status']}, proceeding with caution...")
            else:
                logger.info("Real-Debrid service is healthy")
            
            # Get all available DMM hash lists
            logger.info("Fetching available DMM hash lists...")
            available_hash_lists = await self.dmm.get_available_hash_lists()
            
            if not available_hash_lists:
                logger.warning("No DMM hash lists found, falling back to static hash file")
                # Fallback to static hash file if DMM lists not available
                real_hashes = self.load_real_dmm_hashes()
                if real_hashes:
                    await self.process_hash_batch(real_hashes, "static_file")
                else:
                    logger.error("No hashes available from any source")
                self.save_processed_hashes()
                return
            
            logger.info(f"Found {len(available_hash_lists)} DMM hash lists")
            
            # Limit the number of hash lists to process per run
            hash_list_limit = self.config.get('hash_list_limit', 20)
            if len(available_hash_lists) > hash_list_limit:
                available_hash_lists = available_hash_lists[:hash_list_limit]
                logger.info(f"Limited to {hash_list_limit} hash lists for this run")
            
            # Check for existing torrents in Real-Debrid once
            existing_torrents = await self.check_existing_torrents()
            
            # Process each hash list
            total_added = 0
            total_failed = 0
            total_skipped = 0
            all_results = {'added': [], 'failed': [], 'skipped': []}
            
            for i, hash_list_filename in enumerate(available_hash_lists, 1):
                logger.info(f"Processing hash list {i}/{len(available_hash_lists)}: {hash_list_filename}")
                
                try:
                    # Load hashes from this specific hash list
                    hash_list_hashes = await self.dmm.load_hash_list(hash_list_filename)
                    
                    if not hash_list_hashes:
                        logger.warning(f"No hashes found in {hash_list_filename}, skipping")
                        continue
                    
                    logger.info(f"Loaded {len(hash_list_hashes)} hashes from {hash_list_filename}")
                    
                    # Process this batch of hashes
                    batch_results = await self.process_hash_batch(
                        hash_list_hashes, 
                        hash_list_filename, 
                        existing_torrents
                    )
                    
                    # Accumulate results
                    total_added += len(batch_results['added'])
                    total_failed += len(batch_results['failed'])
                    total_skipped += len(batch_results['skipped'])
                    
                    all_results['added'].extend(batch_results['added'])
                    all_results['failed'].extend(batch_results['failed'])
                    all_results['skipped'].extend(batch_results['skipped'])
                    
                    logger.info(f"Hash list {hash_list_filename} results: "
                              f"Added: {len(batch_results['added'])}, "
                              f"Failed: {len(batch_results['failed'])}, "
                              f"Skipped: {len(batch_results['skipped'])}")
                    
                    # Check if we've reached the max items limit
                    max_items = self.config.get('max_items_per_run', 50)
                    if total_added >= max_items:
                        logger.info(f"Reached maximum items limit ({max_items}), stopping processing")
                        break
                    
                    # Add delay between hash lists to avoid overwhelming the API
                    if i < len(available_hash_lists):
                        logger.info("Waiting 30 seconds before processing next hash list...")
                        await asyncio.sleep(30)
                        
                except Exception as e:
                    logger.error(f"Error processing hash list {hash_list_filename}: {e}")
                    continue
            
            logger.info(f"Total results across all hash lists: "
                       f"Added: {total_added}, Failed: {total_failed}, Skipped: {total_skipped}")
            
            # Send summary notification
            if total_added > 0 or total_failed > 0:
                await self.send_notification(all_results)
            
            # Save processed hashes
            self.save_processed_hashes()
            
        except Exception as e:
            logger.error(f"Error in automation: {str(e)}")
            self.save_processed_hashes()
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
                if self.dmm:
                    await self.dmm.close()
                if self.real_debrid:
                    await self.real_debrid.close()
            except Exception as e:
                logger.debug(f"Error closing sessions: {e}")

    async def process_hash_batch(self, hashes: List[str], source_name: str, existing_torrents: Set[str] = None) -> Dict:
        """Process a batch of hashes from a specific source"""
        if existing_torrents is None:
            existing_torrents = await self.check_existing_torrents()
        
        logger.info(f"Processing {len(hashes)} hashes from {source_name}")
        
        # Parse content from hashes
        content_items = await self.parse_content_from_hashes(hashes)
        logger.info(f"Parsed {len(content_items)} content items from {source_name}")
        
        # Filter content based on preferences  
        filtered_content = self.filter_content(content_items)
        logger.info(f"After filtering: {len(filtered_content)} items from {source_name}")
        
        # Remove already processed hashes
        new_content = [item for item in filtered_content 
                      if item['hash'] not in self.processed_hashes]
        logger.info(f"Found {len(new_content)} new items to process from {source_name}")
        
        if not new_content:
            logger.info(f"No new content to process from {source_name}")
            return {'added': [], 'failed': [], 'skipped': []}
        
        # Remove content that already exists in Real-Debrid
        unique_content = [item for item in new_content 
                         if item['hash'].lower() not in existing_torrents]
        logger.info(f"After removing existing torrents: {len(unique_content)} items remain from {source_name}")
        
        # Remove duplicates within this batch
        unique_content = await self.check_content_similarity(unique_content)
        logger.info(f"After deduplication: {len(unique_content)} unique items from {source_name}")
        
        # Limit items for this batch (distribute the limit across hash lists)
        max_items_total = self.config.get('max_items_per_run', 50)
        max_items_per_batch = max(5, max_items_total // self.config.get('hash_list_limit', 20))
        
        if len(unique_content) > max_items_per_batch:
            unique_content = unique_content[:max_items_per_batch]
            logger.info(f"Limited to {max_items_per_batch} items for this batch from {source_name}")
        
        # Add to Real-Debrid
        if unique_content:
            results = await self.add_content_to_debrid(unique_content, existing_torrents)
            logger.info(f"Batch {source_name} results: "
                       f"Added: {len(results['added'])}, "
                       f"Failed: {len(results['failed'])}, "
                       f"Skipped: {len(results['skipped'])}")
            return results
        else:
            logger.info(f"No unique content to add from {source_name}")
            return {'added': [], 'failed': [], 'skipped': []}
    
    async def run_automation_old(self):
        """Main automation loop with real DMM hashes"""
        logger.info("Starting DebridAuto automation with real DMM hashes")
        
        # Always ensure the processed hashes file exists, even if empty
        self.save_processed_hashes()
        
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
                        # Always save processed hashes file even when aborting
                        self.save_processed_hashes()
                        return
                elif service_status['status'] == 'auth_error':
                    logger.error("Authentication error - check your API key")
                    await self.notifier.send_notification(
                        "❌ DebridAuto Authentication Error",
                        "Invalid API key or authentication failed. Please check your Real-Debrid API key."
                    )
                    # Always save processed hashes file even with auth error
                    self.save_processed_hashes()
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
                # Always save processed hashes file even when no hashes available
                self.save_processed_hashes()
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
                # Always save processed hashes file even when no new content
                self.save_processed_hashes()
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
                # Always save processed hashes file even when no content to add
                self.save_processed_hashes()
            
        except Exception as e:
            logger.error(f"Error in automation: {str(e)}")
            # Always save processed hashes file even on error
            self.save_processed_hashes()
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

    async def parse_content_from_hashes(self, hashes):
        """
        Parse content from torrent hashes using Real-Debrid API
        Returns a list of content items with metadata
        """
        content = []
        
        self.logger.info(f"Parsing {len(hashes)} content items from real hashes")
        
        for torrent_hash in hashes:
            try:
                # Check torrent content before adding to get real file information
                magnet_link = f"magnet:?xt=urn:btih:{torrent_hash}"
                torrent_info = await self.real_debrid.check_torrent_content(torrent_hash)
                
                if not torrent_info or not torrent_info.get('cached', False):
                    self.logger.warning(f"Could not get content info for hash {torrent_hash}")
                    continue
                
                # Extract filenames for content type detection
                files = torrent_info.get("files", [])
                filenames = [file.get("filename", "unknown") for file in files]
                
                if not filenames:
                    self.logger.warning(f"No filenames found for hash {torrent_hash}")
                    continue
                
                # Determine content type based on actual files
                content_type = self.determine_content_type(filenames)
                
                # Calculate total size from files
                total_size = sum(file.get("size", 0) for file in files)
                
                item = {
                    "hash": torrent_hash,
                    "title": f"Cached Content {torrent_hash[:8]}",
                    "type": content_type,
                    "size": total_size,
                    "files": files,
                    "filenames": filenames,
                    "file_count": len(filenames)
                }
                
                content.append(item)
            except Exception as e:
                self.logger.error(f"Error parsing content for hash {torrent_hash}: {e}")
        
        self.logger.info(f"Parsed {len(content)} content items from real hashes")
        return content
    
    def determine_content_type(self, filenames):
        """
        Analyze filenames to determine the type of content (movie, tv, adult, etc.)
        Returns: str - content type ('movie', 'tv', 'adult', 'other')
        """
        if not filenames:
            return "unknown"
            
        # Common adult content indicators in filenames
        adult_keywords = [
            'xxx', 'porn', 'adult', 'sex', 'anal', 'brazzers', 'bangbros', 'naughty', 
            'playboy', 'penthouse', 'hustler', 'x-art', 'mofos', 'blacked', 'reality kings',
            'pornhub', 'xvideos', 'milf', 'mature', 'pussy', 'cock', 'dick', 'nude', 'hardcore'
        ]
        
        # Movie indicators
        movie_keywords = [
            '1080p', '720p', '2160p', 'bdrip', 'brrip', 'bluray', 'webrip', 'dvdrip', 
            'x264', 'x265', 'h264', 'h265', 'hevc', 'remux', 'hdr', 'dts', 'aac', 'atmos'
        ]
        
        # TV show indicators
        tv_keywords = [
            's01', 's02', 's03', 's04', 's05', 'e01', 'e02', 'e03', 'season', 'episode',
            'complete.series', 'complete.season', 'tv.pack'
        ]
        
        # Convert filenames to lowercase for case-insensitive matching
        filenames_lower = [filename.lower() for filename in filenames]
        joined_names = ' '.join(filenames_lower)
        
        # Check for adult content first (most important to filter)
        for keyword in adult_keywords:
            if any(keyword in filename for filename in filenames_lower):
                return "adult"
        
        # Check for TV shows
        for keyword in tv_keywords:
            if any(keyword in filename for filename in filenames_lower):
                return "tv"
        
        # Check for movies
        for keyword in movie_keywords:
            if any(keyword in filename for filename in filenames_lower):
                return "movie"
        
        # Default to other if no specific type detected
        return "other"

    def _extract_quality(self, filenames: List[str]) -> str:
        """Extract quality information from filenames"""
        joined_names = ' '.join(filenames).lower()
        
        # Quality indicators in order of precedence
        quality_indicators = {
            '8k': '8K',
            '4k': '4K',
            '2160p': '4K',
            '1080p': 'FHD',
            '720p': 'HD',
            'bluray': 'BluRay',
            'bdrip': 'BluRay',
            'hdr': 'HDR',
            'webrip': 'WebRip',
            'web-dl': 'WEB-DL',
            'web.dl': 'WEB-DL',
            'dvdrip': 'DVDRip'
        }
        
        for indicator, quality in quality_indicators.items():
            if indicator in joined_names:
                return quality
                
        return ""

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

    def filter_content(self, content_items):
        """
        Filter content based on user preferences and content type
        """
        self.logger.info(f"Filtering {len(content_items)} content items")
        filtered_content = []
        
        for item in content_items:
            # Get content type and other metadata
            content_type = item.get('type', 'unknown')
            hash_value = item.get('hash', '')
            title = item.get('title', f"Content {hash_value[:8]}")
            filenames = item.get('filenames', [])
            
            # Skip adult content
            if content_type == "adult":
                self.logger.info(f"Skipping adult content: {title}")
                # Mark as processed to avoid rechecking in future runs
                self.processed_hashes.add(hash_value)
                continue
                
            # Skip content types not enabled in config
            content_types_config = self.config.get('content_types', {})
            if content_type == "movie" and not content_types_config.get('movies', True):
                self.logger.debug(f"Skipping movie content (disabled in config): {title}")
                continue
                
            if content_type == "tv" and not content_types_config.get('tv_shows', True):
                self.logger.debug(f"Skipping TV content (disabled in config): {title}")
                continue
                
            # Size filtering
            size_bytes = item.get('size', 0)
            size_gb = size_bytes / (1024**3)  # Convert to GB
            
            min_size_gb = self.config.get('min_size_gb', 0.5)
            max_size_gb = self.config.get('max_size_gb', 50.0)
            
            if size_gb < min_size_gb:
                self.logger.debug(f"Skipping content due to small size ({size_gb:.2f} GB): {title}")
                continue
                
            if size_gb > max_size_gb:
                self.logger.debug(f"Skipping content due to large size ({size_gb:.2f} GB): {title}")
                continue
            
            # Keyword filtering
            exclude_keywords = self.config.get('exclude_keywords', [])
            include_keywords = self.config.get('include_keywords', [])
            
            # Convert filenames to lowercase for case-insensitive matching
            filenames_lower = [name.lower() for name in filenames]
            joined_names = ' '.join(filenames_lower)
            
            if any(keyword.lower() in joined_names for keyword in exclude_keywords):
                self.logger.debug(f"Skipping content with excluded keyword: {title}")
                continue
                
            # If include keywords defined, at least one must match
            if include_keywords and not any(keyword.lower() in joined_names for keyword in include_keywords):
                self.logger.debug(f"Skipping content without any include keywords: {title}")
                continue
            
            # All filters passed
            filtered_content.append(item)
        
        self.logger.info(f"Content filtering complete: {len(filtered_content)} of {len(content_items)} items passed filters")
        return filtered_content
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