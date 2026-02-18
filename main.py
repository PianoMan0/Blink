"""
Terminal Video Chat Application
A peer-to-peer video chat application that runs in the terminal
"""

import cv2
import numpy as np
import socket
import threading
import json
import pickle
import argparse
from collections import deque
from datetime import datetime
import sys
from pathlib import Path

class VideoFrameBuffer:
    """Manages frame compression and transmission"""
    
    def __init__(self, max_size=320, quality=80):
        self.max_size = max_size
        self.quality = quality
        
    def compress_frame(self, frame):
        """Compress frame for transmission"""
        if frame is None:
            return None
        
        # Resize for bandwidth efficiency
        h, w = frame.shape[:2]
        if w > self.max_size:
            scale = self.max_size / w
            h = int(h * scale)
            w = self.max_size
            frame = cv2.resize(frame, (w, h))
        
        # Encode to JPEG
        _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        return buffer.tobytes()
    
    def decompress_frame(self, data):
        """Decompress received frame"""
        if data is None:
            return None
        try:
            frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            return frame
        except:
            return None


class VideoCapture:
    """Handles video capture from webcam"""
    
    def __init__(self, device_id=0, fps=15, width=640, height=480):
        self.cap = cv2.VideoCapture(device_id)
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.width = width
        self.height = height
        self.fps = fps
        
    def get_frame(self):
        """Capture a single frame"""
        ret, frame = self.cap.read()
        if ret:
            return cv2.flip(frame, 1)  # Mirror the frame
        return None
    
    def release(self):
        """Release the camera"""
        self.cap.release()
    
    def is_opened(self):
        """Check if camera is available"""
        return self.cap.isOpened()


class SignalingServer:
    """Simple signaling server for peer discovery and connection info exchange"""
    
    def __init__(self, host='127.0.0.1', port=9000):
        self.host = host
        self.port = port
        self.peers = {}
        self.lock = threading.Lock()
        self.server_socket = None
        self.running = False
        
    def start(self):
        """Start the signaling server"""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(5)
        self.running = True
        print(f"\n[SIGNALING SERVER] Started on {self.host}:{self.port}")
        
        while self.running:
            try:
                self.server_socket.settimeout(1)
                client_socket, addr = self.server_socket.accept()
                threading.Thread(target=self._handle_client, args=(client_socket, addr), daemon=True).start()
            except socket.timeout:
                continue
            except:
                if self.running:
                    break
    
    def _handle_client(self, client_socket, addr):
        """Handle a client connection"""
        try:
            data = client_socket.recv(1024).decode('utf-8')
            message = json.loads(data)
            
            if message['type'] == 'register':
                peer_id = message['peer_id']
                with self.lock:
                    self.peers[peer_id] = {
                        'addr': addr,
                        'timestamp': datetime.now()
                    }
                print(f"[SIGNALING SERVER] {peer_id} registered from {addr}")
                response = {'type': 'registered', 'peers': list(self.peers.keys())}
                client_socket.send(json.dumps(response).encode('utf-8'))
            
            elif message['type'] == 'query_peers':
                with self.lock:
                    peers_list = list(self.peers.keys())
                response = {'type': 'peers_list', 'peers': peers_list}
                client_socket.send(json.dumps(response).encode('utf-8'))
            
        except Exception as e:
            print(f"[SIGNALING SERVER] Error handling client {addr}: {e}")
        finally:
            client_socket.close()
    
    def stop(self):
        """Stop the signaling server"""
        self.running = False
        if self.server_socket:
            self.server_socket.close()


class P2PConnection:
    """Manages P2P video stream connection"""
    
    def __init__(self, peer_id, target_peer=None, is_initiator=True):
        self.peer_id = peer_id
        self.target_peer = target_peer
        self.is_initiator = is_initiator
        self.socket = None
        self.connected = False
        self.frame_buffer = VideoFrameBuffer()
        self.received_frames = deque(maxlen=2)
        self.lock = threading.Lock()
        
    def create_server(self, port=9001):
        """Create a server socket for incoming connections"""
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(('0.0.0.0', port))
        self.socket.listen(1)
        print(f"[P2P] Listening for incoming connections on port {port}")
        
        try:
            conn, addr = self.socket.accept()
            self._setup_connection(conn, addr)
        except:
            pass
    
    def connect_to_peer(self, host, port):
        """Connect to a peer"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((host, port))
            print(f"[P2P] Connected to peer at {host}:{port}")
            self.connected = True
        except Exception as e:
            print(f"[P2P] Connection failed: {e}")
            self.connected = False
    
    def _setup_connection(self, sock, addr):
        """Setup an accepted connection"""
        self.socket = sock
        self.connected = True
        print(f"[P2P] Peer connected from {addr}")
        threading.Thread(target=self._receive_frames, daemon=True).start()
    
    def send_frame(self, frame):
        """Send a frame to the peer"""
        if not self.connected or self.socket is None:
            return False
        
        try:
            compressed = self.frame_buffer.compress_frame(frame)
            if compressed:
                # Send frame size first
                size = len(compressed)
                self.socket.sendall(size.to_bytes(4, 'big'))
                # Send frame data
                self.socket.sendall(compressed)
                return True
        except:
            self.connected = False
        
        return False
    
    def _receive_frames(self):
        """Receive frames from peer"""
        while self.connected:
            try:
                # Receive frame size
                size_data = self.socket.recv(4)
                if not size_data:
                    break
                
                size = int.from_bytes(size_data, 'big')
                
                # Receive frame data
                frame_data = b''
                while len(frame_data) < size:
                    chunk = self.socket.recv(min(4096, size - len(frame_data)))
                    if not chunk:
                        break
                    frame_data += chunk
                
                if len(frame_data) == size:
                    frame = self.frame_buffer.decompress_frame(frame_data)
                    if frame is not None:
                        with self.lock:
                            self.received_frames.append(frame)
            except:
                self.connected = False
                break
    
    def get_received_frame(self):
        """Get the latest received frame"""
        with self.lock:
            if self.received_frames:
                return self.received_frames[-1]
        return None
    
    def close(self):
        """Close the connection"""
        self.connected = False
        if self.socket:
            try:
                self.socket.close()
            except:
                pass


class VideoChatUI:
    """Handles video chat UI rendering"""
    
    def __init__(self, local_name="You"):
        self.local_name = local_name
        self.font = cv2.FONT_HERSHEY_SIMPLEX
        self.font_scale = 0.6
        self.font_color = (0, 255, 0)
        self.thickness = 2
    
    def create_composite_view(self, local_frame, remote_frame):
        """Create a composite view of local and remote video"""
        if local_frame is None and remote_frame is None:
            # Create a placeholder
            composite = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(composite, "Waiting for video...", (100, 240), 
                       self.font, 1, self.font_color, self.thickness)
            return composite
        
        if local_frame is None:
            local_h, local_w = remote_frame.shape[:2]
            local_frame = np.zeros((local_h, local_w, 3), dtype=np.uint8)
        
        if remote_frame is None:
            remote_h, remote_w = local_frame.shape[:2]
            remote_frame = np.zeros((remote_h, remote_w, 3), dtype=np.uint8)
        
        # Resize frames to standard size
        target_h, target_w = 480, 640
        local_resized = cv2.resize(local_frame, (target_w // 2, target_h))
        remote_resized = cv2.resize(remote_frame, (target_w // 2, target_h))
        
        # Create composite (side by side)
        composite = np.hstack([local_resized, remote_resized])
        
        # Add labels
        cv2.putText(composite, self.local_name, (10, 30), self.font, 0.7, self.font_color, 2)
        cv2.putText(composite, "Remote", (target_w // 2 + 10, 30), self.font, 0.7, (0, 0, 255), 2)
        
        return composite
    
    def add_status(self, frame, status_text):
        """Add status text to frame"""
        cv2.putText(frame, status_text, (10, frame.shape[0] - 10), 
                   self.font, 0.5, (255, 255, 255), 1)
        return frame


class VideoChatApp:
    """Main video chat application"""
    
    def __init__(self, peer_id, mode='client', target_host=None, target_port=None):
        self.peer_id = peer_id
        self.mode = mode
        self.target_host = target_host
        self.target_port = target_port
        self.video_capture = None
        self.connection = None
        self.running = False
        self.ui = VideoChatUI(peer_id)
        
    def initialize(self):
        """Initialize the application"""
        print(f"\n[APP] Initializing video chat for peer: {self.peer_id}")
        
        # Initialize video capture
        self.video_capture = VideoCapture()
        if not self.video_capture.is_opened():
            print("[ERROR] Could not open video device. Make sure a webcam is available.")
            return False
        
        # Initialize P2P connection
        self.connection = P2PConnection(self.peer_id, is_initiator=(self.mode == 'initiator'))
        
        if self.mode == 'server':
            # Start server to accept connections
            threading.Thread(target=self.connection.create_server, args=(9001,), daemon=True).start()
        elif self.mode == 'client' and self.target_host and self.target_port:
            # Connect to peer
            self.connection.connect_to_peer(self.target_host, int(self.target_port))
        
        self.running = True
        return True
    
    def run(self):
        """Run the main video chat loop"""
        if not self.initialize():
            return
        
        print("\n[APP] Video chat started!")
        print("[APP] Press 'q' to quit, 's' to save screenshot")
        print("[APP] Waiting for connection...\n")
        
        frame_count = 0
        
        try:
            while self.running:
                # Capture local frame
                local_frame = self.video_capture.get_frame()
                
                # Send frame to peer
                if local_frame is not None and self.connection.connected:
                    self.connection.send_frame(local_frame)
                
                # Receive frame from peer
                remote_frame = self.connection.get_received_frame()
                
                # Create composite view
                composite = self.ui.create_composite_view(local_frame, remote_frame)
                
                # Add connection status
                status = "Connected" if self.connection.connected else "Waiting..."
                composite = self.ui.add_status(composite, status)
                
                # Display
                cv2.imshow('Terminal Video Chat', composite)
                
                # Handle key input
                key = cv2.waitKey(1) & 0xFF
                if key == ord('q'):
                    print("\n[APP] Quitting...")
                    break
                elif key == ord('s'):
                    filename = f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                    cv2.imwrite(filename, composite)
                    print(f"[APP] Screenshot saved: {filename}")
                
                frame_count += 1
                if frame_count % 30 == 0:
                    print(f"[APP] Frames processed: {frame_count}, Connected: {self.connection.connected}")
        
        except KeyboardInterrupt:
            print("\n[APP] Interrupted by user")
        finally:
            self.shutdown()
    
    def shutdown(self):
        """Shutdown the application"""
        print("\n[APP] Shutting down...")
        self.running = False
        
        if self.connection:
            self.connection.close()
        
        if self.video_capture:
            self.video_capture.release()
        
        cv2.destroyAllWindows()
        print("[APP] Video chat closed.")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='Terminal Video Chat Application')
    parser.add_argument('--peer-id', type=str, default=f'peer_{datetime.now().strftime("%H%M%S")}',
                       help='Unique peer identifier')
    parser.add_argument('--mode', type=str, choices=['server', 'client'], default='server',
                       help='Run as server (listen) or client (connect)')
    parser.add_argument('--host', type=str, default='127.0.0.1',
                       help='Target host for client mode')
    parser.add_argument('--port', type=int, default=9001,
                       help='Target port for client mode or listen port for server mode')
    
    args = parser.parse_args()
    
    # Create and run app
    app = VideoChatApp(
        peer_id=args.peer_id,
        mode=args.mode,
        target_host=args.host if args.mode == 'client' else None,
        target_port=args.port if args.mode == 'client' else None
    )
    
    app.run()


if __name__ == '__main__':
    main()