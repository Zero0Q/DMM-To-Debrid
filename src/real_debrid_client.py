"""
Real-Debrid API Client for managing downloads and torrents
"""
import aiohttp
import asyncio
from typing import Dict, List, Optional
import logging
import json

logger = logging.getLogger(__name__)

class RealDebridClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.real-debrid.com/rest/1.0"
        self.session = None
        self._closed = False
        
    async def __aenter__(self):
        """Async context manager entry"""
        if not self.session and not self._closed:
            self.session = aiohttp.ClientSession()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()
    
    async def close(self):
        """Explicitly close the session"""
        if self.session and not self._closed:
            await self.session.close()
            self._closed = True
            self.session = None
    
    async def _get_session(self):
        """Get or create session"""
        if not self.session and not self._closed:
            self.session = aiohttp.ClientSession()
        elif self._closed:
            raise RuntimeError("Client has been closed")
        return self.session
    
    async def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict:
        """Make authenticated request to Real-Debrid API with improved error handling"""
        session = await self._get_session()
        
        # Use headers that better match web browsers and DMM
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Origin': 'https://real-debrid.com',
            'Referer': 'https://real-debrid.com/',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache'
        }
        
        # Merge any custom headers
        if 'headers' in kwargs:
            headers.update(kwargs.pop('headers'))
        
        url = f"{self.base_url}/{endpoint}"
        
        # Add random delay to mimic human behavior
        await asyncio.sleep(0.5 + (hash(url) % 100) / 1000)  # 0.5-0.6s delay
        
        try:
            async with session.request(method, url, headers=headers, **kwargs) as response:
                response_text = await response.text()
                
                # Log response for debugging
                logger.debug(f"Real-Debrid API {method} {endpoint}: {response.status}")
                
                # Success responses: 200 OK, 201 Created, 204 No Content
                if response.status in [200, 201, 204]:
                    try:
                        if response_text:
                            return json.loads(response_text)
                        else:
                            return {'success': True, 'status': response.status}
                    except json.JSONDecodeError:
                        return {'raw_response': response_text, 'success': True, 'status': response.status}
                
                # Handle error responses with detailed information
                try:
                    error_data = json.loads(response_text) if response_text else {}
                    error_code = error_data.get('error_code', 'unknown')
                    error_message = error_data.get('error', 'Unknown error')
                    
                    logger.error(f"Real-Debrid API error {response.status}: {error_code} - {error_message}")
                    logger.error(f"Request URL: {url}")
                    logger.error(f"Request data: {kwargs.get('data', 'None')}")
                    
                    raise Exception(f"Real-Debrid API error {response.status}: {error_code} - {error_message}")
                    
                except json.JSONDecodeError:
                    logger.error(f"Real-Debrid API error {response.status}: {response_text}")
                    raise Exception(f"Real-Debrid API error {response.status}: {response_text}")
                    
        except aiohttp.ClientError as e:
            logger.error(f"Network error communicating with Real-Debrid: {str(e)}")
            raise Exception(f"Network error: {str(e)}")
    
    async def add_magnet(self, magnet_link: str) -> bool:
        """Add magnet link to Real-Debrid with improved validation and retry logic"""
        max_retries = 5  # Increased retries
        base_retry_delay = 3  # Longer initial delay
        
        for attempt in range(max_retries):
            try:
                # Validate magnet link format
                if not self._validate_magnet_link(magnet_link):
                    logger.error(f"Invalid magnet link format: {magnet_link}")
                    return False
                
                logger.info(f"Adding magnet to Real-Debrid (attempt {attempt + 1}/{max_retries}): {magnet_link[:100]}...")
                
                # Prepare form data like a browser would
                data = aiohttp.FormData()
                data.add_field('magnet', magnet_link)
                
                # Use POST with form data instead of JSON
                response = await self._make_request('POST', 'torrents/addMagnet', data=data)
                
                # Check for successful addition - Real-Debrid returns 201 with torrent info
                if response and ('id' in response or 'uri' in response):
                    torrent_id = response.get('id')
                    torrent_uri = response.get('uri', '')
                    
                    if torrent_id:
                        logger.info(f"Successfully added magnet to Real-Debrid! Torrent ID: {torrent_id}")
                        if torrent_uri:
                            logger.info(f"Torrent info URL: {torrent_uri}")
                        
                        # Wait a moment before selecting files
                        await asyncio.sleep(1)
                        
                        # Optionally select files (Real-Debrid usually auto-selects)
                        try:
                            await self._select_files(torrent_id)
                        except Exception as e:
                            logger.warning(f"Could not select files for torrent {torrent_id}: {e}")
                        
                        return True
                    else:
                        logger.warning(f"Torrent added but no ID returned: {response}")
                        return True  # Still consider it successful if we got a response
                elif response and response.get('success'):
                    logger.info("Successfully added magnet to Real-Debrid (no ID returned)")
                    return True
                else:
                    logger.error(f"Unexpected response when adding magnet: {response}")
                    if attempt < max_retries - 1:
                        retry_delay = base_retry_delay * (1.5 ** attempt)  # Slower exponential backoff
                        logger.info(f"Retrying in {retry_delay:.1f} seconds...")
                        await asyncio.sleep(retry_delay)
                        continue
                    return False
                    
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Failed to add magnet (attempt {attempt + 1}/{max_retries}): {error_msg}")
                
                # Check if it's a server error (503, 502, 504, rate limits, etc.)
                is_server_error = any(code in error_msg for code in ["503", "502", "504", "429", "internal_error", "timeout", "Network error"])
                
                if is_server_error and attempt < max_retries - 1:
                    # Use longer delays and jitter for server errors
                    retry_delay = base_retry_delay * (1.8 ** attempt) + (hash(magnet_link) % 3)  # 3-6s, 5-8s, 9-12s etc
                    logger.info(f"Server error detected ({error_msg}), retrying in {retry_delay:.1f} seconds...")
                    await asyncio.sleep(retry_delay)
                    continue
                
                # For other errors (auth, invalid magnet, etc.), don't retry
                if "401" in error_msg or "403" in error_msg:
                    logger.error("Authentication error - check your Real-Debrid API key")
                elif "400" in error_msg:
                    logger.error("Bad request - possibly invalid magnet link")
                
                return False
        
        logger.error(f"Failed to add magnet after {max_retries} attempts")
        return False
    
    async def add_torrent(self, magnet_link: str) -> bool:
        """Add torrent using magnet link - alias for add_magnet for compatibility"""
        return await self.add_magnet(magnet_link)
    
    def _validate_magnet_link(self, magnet_link: str) -> bool:
        """Validate magnet link format"""
        if not magnet_link.startswith('magnet:?'):
            return False
        
        # Check for required xt parameter (torrent hash)
        if 'xt=urn:btih:' not in magnet_link:
            return False
        
        # Extract hash and validate length
        try:
            hash_part = magnet_link.split('xt=urn:btih:')[1].split('&')[0]
            # Accept Base32 (32), SHA-1 hex (40), or SHA-256 hex (64) hashes
            if len(hash_part) not in [32, 40, 64]:
                logger.error(f"Invalid hash length {len(hash_part)}, expected 32, 40, or 64 characters")
                return False
        except (IndexError, ValueError):
            return False
        
        return True
    
    async def _select_files(self, torrent_id: str):
        """Select all files in a torrent"""
        try:
            # Get torrent info to see available files
            torrent_info = await self._make_request('GET', f'torrents/info/{torrent_id}')
            
            if torrent_info and 'files' in torrent_info:
                files = torrent_info['files']
                if files:
                    # Select all files
                    file_ids = ','.join(str(f['id']) for f in files)
                    data = {'files': file_ids}
                    await self._make_request('POST', f'torrents/selectFiles/{torrent_id}', data=data)
                    logger.info(f"Selected {len(files)} files for torrent {torrent_id}")
                    
        except Exception as e:
            logger.warning(f"Could not select files for torrent {torrent_id}: {e}")
    
    async def get_torrents(self) -> List[Dict]:
        """Get list of torrents"""
        try:
            return await self._make_request('GET', 'torrents') or []
        except Exception as e:
            logger.error(f"Failed to get torrents: {e}")
            return []
    
    async def get_downloads(self) -> List[Dict]:
        """Get list of downloads"""
        try:
            return await self._make_request('GET', 'downloads') or []
        except Exception as e:
            logger.error(f"Failed to get downloads: {e}")
            return []
    
    async def delete_torrent(self, torrent_id: str) -> bool:
        """Delete a torrent"""
        try:
            result = await self._make_request('DELETE', f'torrents/delete/{torrent_id}')
            return result is not None
        except Exception as e:
            logger.error(f"Failed to delete torrent {torrent_id}: {e}")
            return False
    
    async def get_torrent_info(self, hash_str: str) -> Optional[Dict]:
        """Get torrent information by hash"""
        # Real-Debrid doesn't have a direct hash lookup endpoint
        # We'll simulate torrent info for the auto-add process
        try:
            # For now, return basic info to allow the auto-add process to continue
            # The actual availability will be checked when we try to add the magnet
            return {
                'hash': hash_str,
                'filename': f"content_{hash_str[:8]}",  # Placeholder filename
                'bytes': 1073741824,  # 1GB placeholder size
                'status': 'unknown',
                'available': True  # Assume available to allow processing
            }
                
        except Exception as e:
            logger.debug(f"Could not check availability for hash {hash_str}: {str(e)}")
            return None
    
    async def check_service_status(self) -> Dict:
        """Check Real-Debrid service status and API health"""
        try:
            # Check user account to verify API is working
            user_info = await self._make_request('GET', 'user')
            if user_info:
                return {
                    'status': 'healthy',
                    'api_responsive': True,
                    'user': user_info.get('username', 'unknown'),
                    'premium_until': user_info.get('premium', 'unknown')
                }
        except Exception as e:
            error_msg = str(e)
            status = 'unhealthy'
            
            if "503" in error_msg or "502" in error_msg or "504" in error_msg:
                status = 'service_unavailable'
            elif "401" in error_msg or "403" in error_msg:
                status = 'auth_error'
            elif "429" in error_msg:
                status = 'rate_limited'
            
            return {
                'status': status,
                'api_responsive': False,
                'error': error_msg
            }
    
    async def wait_for_service_recovery(self, max_wait_minutes: int = 30) -> bool:
        """Wait for Real-Debrid service to recover from 503 errors"""
        logger.info(f"Waiting for Real-Debrid service recovery (max {max_wait_minutes} minutes)...")
        
        check_interval = 60  # Check every minute
        max_checks = max_wait_minutes
        
        for attempt in range(max_checks):
            try:
                status = await self.check_service_status()
                if status['status'] == 'healthy':
                    logger.info("Real-Debrid service has recovered!")
                    return True
                elif status['status'] == 'auth_error':
                    logger.error("Authentication error - service recovery won't help")
                    return False
                    
                logger.info(f"Service still {status['status']}, checking again in {check_interval}s...")
                await asyncio.sleep(check_interval)
                
            except Exception as e:
                logger.debug(f"Status check failed: {e}")
                await asyncio.sleep(check_interval)
        
        logger.warning(f"Service did not recover within {max_wait_minutes} minutes")
        return False