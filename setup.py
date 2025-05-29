#!/usr/bin/env python3
"""
Setup script for DebridAuto
Helps users configure the project and test their Real-Debrid API key
"""

import os
import json
import asyncio
import aiohttp
from pathlib import Path

async def test_api_key(api_key):
    """Test Real-Debrid API key"""
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {api_key}"}
            async with session.get("https://api.real-debrid.com/rest/1.0/user", headers=headers) as response:
                if response.status == 200:
                    user_data = await response.json()
                    return True, user_data
                else:
                    return False, f"HTTP {response.status}"
    except Exception as e:
        return False, str(e)

def setup_environment():
    """Set up environment and directories"""
    print("🚀 DebridAuto Setup\n")
    
    # Ensure directories exist
    Path('logs').mkdir(exist_ok=True)
    Path('data').mkdir(exist_ok=True)
    Path('config').mkdir(exist_ok=True)
    
    # Check if real_dmm_hashes.json exists
    if not Path('real_dmm_hashes.json').exists():
        print("❌ real_dmm_hashes.json not found!")
        print("This file contains the real DMM torrent hashes.")
        print("Make sure it's in the project root directory.")
        return False
    
    # Load and verify hashes
    try:
        with open('real_dmm_hashes.json') as f:
            data = json.load(f)
        hashes = data.get('hashes', [])
        print(f"✅ Found {len(hashes)} real DMM hashes")
    except Exception as e:
        print(f"❌ Error reading hash file: {e}")
        return False
    
    return True

async def main():
    """Main setup function"""
    if not setup_environment():
        return
    
    # Get API key from user
    api_key = input("\n🔑 Enter your Real-Debrid API key: ").strip()
    
    if not api_key:
        print("❌ No API key provided")
        return
    
    print("\n🔍 Testing API key...")
    success, result = await test_api_key(api_key)
    
    if success:
        print(f"✅ API key valid!")
        print(f"👤 User: {result.get('username', 'Unknown')}")
        print(f"📅 Premium until: {result.get('expiration', 'Unknown')}")
        
        # Save API key to environment file
        with open('.env', 'w') as f:
            f.write(f"REAL_DEBRID_API_KEY={api_key}\n")
        print("💾 API key saved to .env file")
        
        print("\n🎉 Setup complete!")
        print("\nNext steps:")
        print("1. Review config/settings.yml for your preferences")
        print("2. Run: python3 src/main.py")
        print("3. Check logs/ for automation results")
        
    else:
        print(f"❌ API key test failed: {result}")
        print("Please check your API key and try again")

if __name__ == "__main__":
    asyncio.run(main())