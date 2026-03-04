"""
Alternative build script - builds exe WITH console window
Useful for debugging and seeing error messages

Usage:
    python build_exe_with_console.py
"""

import os
import subprocess
import sys
from pathlib import Path

def build_exe():
    print("="*70)
    print("Music Player V2 - Building EXE (WITH CONSOLE)")
    print("="*70)
    
    # Check if PyInstaller is installed
    try:
        import PyInstaller
        print("✓ PyInstaller found")
    except ImportError:
        print("✗ PyInstaller not found")
        print("\nInstalling PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
        print("✓ PyInstaller installed")
    
    # Build command
    script_path = Path(__file__).parent / "music_player.py"
    
    if not script_path.exists():
        print(f"✗ Error: {script_path} not found!")
        return
    
    print(f"\n✓ Found: {script_path}")
    print("\nBuilding executable...")
    print("This may take 2-3 minutes...\n")
    
    # PyInstaller command - NO --windowed flag = console stays visible
    cmd = [
        "pyinstaller",
        "--name=MusicPlayerV2_Console",    # Output name
        "--onefile",                        # Single exe file
        "--clean",                          # Clean cache
        str(script_path)
    ]
    
    # Run PyInstaller
    try:
        subprocess.check_call(cmd)
        
        print("\n" + "="*70)
        print("✓ BUILD SUCCESSFUL!")
        print("="*70)
        
        exe_path = Path("dist") / "MusicPlayerV2_Console.exe"
        if exe_path.exists():
            size_mb = exe_path.stat().st_size / (1024 * 1024)
            print(f"\n✓ Executable created: {exe_path.absolute()}")
            print(f"  Size: {size_mb:.1f} MB")
            print(f"\nThis version SHOWS the console window")
            print(f"Use this for debugging or if you want to see status messages")
        
    except subprocess.CalledProcessError as e:
        print(f"\n✗ Build failed: {e}")
        return False
    
    return True

if __name__ == "__main__":
    success = build_exe()
    
    if success:
        input("\nPress Enter to exit...")
    else:
        input("\nBuild failed. Press Enter to exit...")
