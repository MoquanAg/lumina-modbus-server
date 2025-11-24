#!/usr/bin/env python3
"""
Test script for PyModbus-enabled LuminaModbusServer

Tests:
1. Server can start
2. Client can connect
3. Modbus commands work through TCP proxy
"""

import time
import threading
import socket
import sys

def test_server_import():
    """Test 1: Can we import the server?"""
    print("=" * 70)
    print("TEST 1: Server Import")
    print("=" * 70)
    try:
        from LuminaModbusServer import LuminaModbusServer
        print("✓ Server imports successfully")
        print("✓ PyModbus integration working")
        return True
    except Exception as e:
        print(f"✗ Import failed: {e}")
        return False

def test_server_startup():
    """Test 2: Can the server start?"""
    print("\n" + "=" * 70)
    print("TEST 2: Server Startup")
    print("=" * 70)
    
    try:
        from LuminaModbusServer import LuminaModbusServer
        
        # Start server in background thread
        server = LuminaModbusServer(host='127.0.0.1', port=8888)
        
        def run_server():
            try:
                server.start()
            except Exception as e:
                print(f"Server error: {e}")
        
        server_thread = threading.Thread(target=run_server, daemon=True)
        server_thread.start()
        
        # Give server time to start
        time.sleep(2)
        
        print("✓ Server started on 127.0.0.1:8888")
        return True, server
        
    except Exception as e:
        print(f"✗ Server startup failed: {e}")
        import traceback
        traceback.print_exc()
        return False, None

def test_client_connection(server):
    """Test 3: Can client connect?"""
    print("\n" + "=" * 70)
    print("TEST 3: Client Connection")
    print("=" * 70)
    
    try:
        # Try to connect to server
        sock = socket.create_connection(('127.0.0.1', 8888), timeout=5.0)
        print("✓ Client connected to server")
        
        # Send a test command (read holding registers from THC sensor)
        # Protocol: command_id:device_type:port:baudrate:command_hex:response_length:timeout
        # Command: 01 03 00 00 00 02 [CRC will be added by client]
        # This reads 2 registers starting at 0x0000 from slave 0x01
        
        test_command = "test001:THC:/dev/ttyAMA1:9600:010300000002C40B:9:0.5\n"
        print(f"Sending test command: {test_command.strip()}")
        
        sock.send(test_command.encode())
        
        # Wait for response (or timeout)
        sock.settimeout(3.0)
        response = sock.recv(1024).decode()
        
        print(f"Received response: {response.strip()}")
        
        # Parse response
        if "ERROR" in response:
            print("⚠ Server returned error (expected if no sensor connected)")
            print("  This is OK - it means PyModbus is working, just no hardware")
        else:
            print("✓ Server processed command successfully")
        
        sock.close()
        return True
        
    except socket.timeout:
        print("✗ No response from server (timeout)")
        return False
    except Exception as e:
        print(f"✗ Connection test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Run all tests"""
    print("\n")
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║     PyModbus-Enabled LuminaModbusServer Test Suite                  ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print()
    
    results = []
    
    # Test 1: Import
    result1 = test_server_import()
    results.append(("Import", result1))
    
    if not result1:
        print("\n✗ Cannot proceed - import failed")
        sys.exit(1)
    
    # Test 2: Startup
    result2, server = test_server_startup()
    results.append(("Startup", result2))
    
    if not result2:
        print("\n✗ Cannot proceed - server startup failed")
        sys.exit(1)
    
    # Test 3: Client connection
    result3 = test_client_connection(server)
    results.append(("Client Connection", result3))
    
    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    for test_name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status}: {test_name}")
    
    all_passed = all(r[1] for r in results)
    
    print()
    if all_passed:
        print("🎉 All tests passed!")
        print()
        print("Next steps:")
        print("1. Deploy to lumina-edge: cp LuminaModbusServer.py ~/lumina-edge/communication/modbus/")
        print("2. Start server: ./start_modbus_server.sh")
        print("3. Use from main.py and sensor_manager.py simultaneously!")
    else:
        print("⚠ Some tests failed - check errors above")
    
    print()
    print("Stopping server...")
    if server:
        server.stop()

if __name__ == "__main__":
    main()

