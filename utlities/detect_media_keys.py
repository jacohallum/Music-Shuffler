"""
Media Key Detector
Press your F5-F8 keys (with Fn lock ON) to see their key names
Press ESC to exit
"""

import keyboard

print("="*60)
print("MEDIA KEY DETECTOR")
print("="*60)
print("\nPress your F5, F6, F7, F8 keys (with Fn lock ON)")
print("Write down the 'name' shown for each key")
print("Press ESC to exit")
print("="*60)
print()

detected_keys = {}

def on_key(event):
    if event.event_type == 'down':
        key_info = f"Key: {event.name:20} | Scan Code: {event.scan_code:3} | VK: {event.vk if hasattr(event, 'vk') else 'N/A'}"
        
        # Track unique keys
        if event.name not in detected_keys:
            detected_keys[event.name] = event.scan_code
            print(f"✓ {key_info}")
        
        # Exit on ESC
        if event.name == 'esc':
            print("\n" + "="*60)
            print("SUMMARY - Use these key names in your code:")
            print("="*60)
            for name, code in detected_keys.items():
                if name != 'esc':
                    print(f"  '{name}'  (scan code: {code})")
            print("="*60)
            keyboard.unhook_all()
            exit()

keyboard.hook(on_key)
keyboard.wait()
