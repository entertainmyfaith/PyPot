#!/usr/bin/env python3
import logging
from logging.handlers import RotatingFileHandler
import paramiko
import threading
import time
import socket
import select
from pathlib import Path
import sys

# ====================== CONFIGURATION ======================
SSH_BANNER = "SSH-2.0-OpenSSH_7.9p1 Ubuntu-10"
BASE_DIR = Path(__file__).resolve().parent.parent

SERVER_KEY_PATH = BASE_DIR / 'HoneyPy' / 'static' / 'server.key'
CREDS_LOG_PATH = BASE_DIR / 'HoneyPy' / 'log_files' / 'creds_audits.log'
COMMANDS_LOG_PATH = BASE_DIR / 'HoneyPy' / 'log_files' / 'cmd_audits.log'

# ====================== SETUP ======================
try:
    HOST_KEY = paramiko.RSAKey(filename=str(SERVER_KEY_PATH))
except FileNotFoundError:
    raise FileNotFoundError(f"Server key not found at {SERVER_KEY_PATH}")

def setup_logger(name, log_file, max_bytes=1000000, backup_count=3):
    """Configure rotating log files"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        log_file, 
        maxBytes=max_bytes, 
        backupCount=backup_count,
        encoding='utf-8'
    )
    handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    logger.addHandler(handler)
    return logger

CREDS_LOGGER = setup_logger('CredsLogger', CREDS_LOG_PATH)
COMMANDS_LOGGER = setup_logger('CommandLogger', COMMANDS_LOG_PATH)

# ====================== SHELL EMULATION ======================
class RealisticShell:
    def __init__(self, channel, client_ip):
        self.channel = channel
        self.client_ip = client_ip
        self.command_history = []
        self.history_pos = 0
        self.current_cmd = ""
        self.cursor_pos = 0
        self.current_dir = "/home/pineapple"
        
        # Terminal control sequences
        self.CRLF = b"\r\n"
        self.BACKSPACE = b"\x08 \x08"
        self.CLEAR_LINE = b"\x1b[2K\r"
        self.CURSOR_LEFT = b"\x1b[D"
        self.CURSOR_RIGHT = b"\x1b[C"
        self.CLEAR_SCREEN = b"\x1b[2J\x1b[H"
        
        # Fake filesystem structure
        self.fake_fs = {
            "/": {
                "type": "dir",
                "contents": {
                    "bin": {"type": "dir", "contents": ["bash", "ls", "cat", "clear"]},
                    "etc": {
                        "type": "dir", 
                        "contents": {
                            "passwd": {"type": "file", "content": "root:x:0:0:root:/root:/bin/bash\npineapple:x:1000:1000:,,,:/home/pineapple:/bin/bash"},
                            "shadow": {"type": "file", "content": "root:!:18659:0:99999:7:::\npineapple:!:18659:0:99999:7:::"},
                            "network": {
                                "type": "dir",
                                "contents": {
                                    "interfaces": {"type": "file", "content": "auto lo\niface lo inet loopback"}
                                }
                            }
                        }
                    },
                    "home": {
                        "type": "dir",
                        "contents": {
                            "pineapple": {
                                "type": "dir",
                                "contents": {
                                    ".bash_history": {"type": "file", "content": "ls\ncd documents\ncat todo.txt"},
                                    ".ssh": {
                                        "type": "dir",
                                        "contents": {
                                            "authorized_keys": {"type": "file", "content": ""},
                                            "id_rsa": {"type": "file", "content": "-----BEGIN RSA PRIVATE KEY-----\n...fake key data...\n-----END RSA PRIVATE KEY-----"}
                                        }
                                    },
                                    "documents": {
                                        "type": "dir",
                                        "contents": {
                                            "todo.txt": {"type": "file", "content": "1. Finish project\n2. Backup files\n3. Update server"},
                                            "projects": {
                                                "type": "dir",
                                                "contents": ["website", "scripts"]
                                            }
                                        }
                                    },
                                    "Downloads": {
                                        "type": "dir",
                                        "contents": ["archive.tar.gz", "package.deb"]
                                    }
                                }
                            }
                        }
                    },
                    "var": {
                        "type": "dir",
                        "contents": {
                            "log": {
                                "type": "dir",
                                "contents": ["auth.log", "syslog"]
                            },
                            "www": {
                                "type": "dir",
                                "contents": ["index.html"]
                            }
                        }
                    },
                    "usr": {
                        "type": "dir",
                        "contents": {
                            "bin": {"type": "dir", "contents": ["python3", "gcc"]},
                            "lib": {"type": "dir", "contents": []}
                        }
                    }
                }
            }
        }

    def send(self, data):
        """Wrapper for channel.send with error handling"""
        try:
            if isinstance(data, str):
                data = data.encode('utf-8')
            self.channel.send(data)
        except:
            pass

    def show_prompt(self):
        """Display the shell prompt with bright cyan username"""
        short_path = self._get_short_path()
        self.send(f"\x1b[96mpineapple@honeypot\x1b[0m:\x1b[34m{short_path}\x1b[0m$ ")

    def _get_short_path(self):
        """Convert full path to shortened prompt version"""
        if self.current_dir == "/":
            return "/"
        if self.current_dir == "/home/pineapple":
            return "~"
        if self.current_dir.startswith("/home/pineapple/"):
            return "~/" + self.current_dir[16:]
        return self.current_dir

    def _get_directory_contents(self, path):
        """Fixed: Returns directory contents without duplicates"""
        if path == "~":
            path = "/home/pineapple"
        elif path.startswith("~/"):
            path = "/home/pineapple" + path[1:]
        
        parts = [p for p in path.split('/') if p]
        current = self.fake_fs['/']['contents']
        
        for part in parts:
            if part in current and current[part]['type'] == 'dir':
                current = current[part]['contents']
            else:
                return []
        
        # Use dict.fromkeys() to remove duplicates while preserving order
        contents = list(dict.fromkeys(
            [k for k in current.keys() if isinstance(current[k], dict)] +
            [item for item in current if not isinstance(item, dict)]
        ))
        return contents

    def handle_special_keys(self, data):
        """Process terminal control sequences"""
        if data == b"\x7f":  # Backspace
            if self.cursor_pos > 0:
                self.current_cmd = self.current_cmd[:self.cursor_pos-1] + self.current_cmd[self.cursor_pos:]
                self.cursor_pos -= 1
                self.send(self.BACKSPACE)
            return True
            
        elif data == b"\x1b":  # Escape sequence
            seq = self.channel.recv(2)
            if seq == b"[A":  # Up arrow
                if self.command_history and self.history_pos > 0:
                    self.history_pos -= 1
                    self.current_cmd = self.command_history[self.history_pos]
                    self.cursor_pos = len(self.current_cmd)
                    self.send(self.CLEAR_LINE)
                    self.show_prompt()
                    self.send(self.current_cmd.encode())
                return True
                
            elif seq == b"[B":  # Down arrow
                if self.command_history and self.history_pos < len(self.command_history)-1:
                    self.history_pos += 1
                    self.current_cmd = self.command_history[self.history_pos]
                    self.cursor_pos = len(self.current_cmd)
                    self.send(self.CLEAR_LINE)
                    self.show_prompt()
                    self.send(self.current_cmd.encode())
                return True
                
            elif seq == b"[C":  # Right arrow
                if self.cursor_pos < len(self.current_cmd):
                    self.cursor_pos += 1
                    self.send(self.CURSOR_RIGHT)
                return True
                
            elif seq == b"[D":  # Left arrow
                if self.cursor_pos > 0:
                    self.cursor_pos -= 1
                    self.send(self.CURSOR_LEFT)
                return True
                
        elif data == b"\t":  # Tab completion
            current_path = self.current_cmd.split()[-1] if self.current_cmd.split() else ""
            dirs = self._get_directory_contents(self.current_dir)
            matches = [d for d in dirs if d.startswith(current_path)]
            if matches:
                completion = matches[0][len(current_path):]
                self.current_cmd += completion
                self.cursor_pos = len(self.current_cmd)
                self.send(completion.encode())
            return True
            
        return False

    def _resolve_path(self, path):
        """Convert relative path to absolute path"""
        if path.startswith("/"):
            return path
        if path.startswith("~"):
            return "/home/pineapple" + path[1:]
        if path == ".":
            return self.current_dir
        if path == "..":
            if self.current_dir == "/":
                return "/"
            return "/".join(self.current_dir.split("/")[:-1]) or "/"
        return self.current_dir.rstrip("/") + "/" + path

    def execute_command(self, command):
        """Process and respond to commands"""
        command = command.strip()
        if not command:
            return ""
            
        # Log the command before execution
        COMMANDS_LOGGER.info(f"IP: {self.client_ip} | Command: {command}")
        
        # Add to history if not duplicate
        if not self.command_history or command != self.command_history[-1]:
            self.command_history.append(command)
        self.history_pos = len(self.command_history)
        
        # Handle common commands
        if command == "exit":
            return "logout"
            
        elif command == "help":
            return (
                "Available commands:\n"
                "  ls, cd, pwd, whoami, id, uname, cat, clear\n"
                "  exit - disconnect from the server"
            )
            
        elif command == "clear":
            self.send(self.CLEAR_SCREEN)
            return ""
            
        elif command.startswith("ls"):
            args = command[2:].strip().split()
            path = self.current_dir if not args else args[0]
            abs_path = self._resolve_path(path)
            dirs = self._get_directory_contents(abs_path)
            
            if not dirs:
                return f"ls: cannot access '{path}': No such file or directory"
                
            # Format with colors like real ls
            colored_dirs = []
            for item in dirs:
                if self._is_directory(abs_path + "/" + item):
                    colored_dirs.append(f"\x1b[34m{item}\x1b[0m")  # Blue for directories
                else:
                    colored_dirs.append(item)
            return "  ".join(colored_dirs)
            
        elif command.startswith("cd "):
            path = command[3:].strip()
            if not path or path == "~":
                self.current_dir = "/home/pineapple"
                return ""
                
            abs_path = self._resolve_path(path)
            
            if abs_path == "/":
                self.current_dir = "/"
                return ""
                
            if path in ["..", "../"]:
                if self.current_dir != "/":
                    self.current_dir = "/".join(self.current_dir.split("/")[:-1]) or "/"
                return ""
                
            if not self._get_directory_contents(abs_path):
                return f"-bash: cd: {path}: No such file or directory"
                
            self.current_dir = abs_path
            return ""
            
        elif command == "pwd":
            return self.current_dir
            
        elif command == "whoami":
            return "pineapple"
            
        elif command == "id":
            return "uid=1000(pineapple) gid=1000(pineapple) groups=1000(pineapple)"
            
        elif command == "uname -a":
            return "Linux honeypot 4.15.0-112-generic #113-Ubuntu SMP x86_64 GNU/Linux"
            
        elif command.startswith("sudo"):
            return "pineapple is not in the sudoers file. This incident will be reported."
            
        elif command.startswith("cat "):
            filename = command[4:].strip()
            abs_path = self._resolve_path(filename)
            return self._get_file_content(abs_path)
            
        return f"bash: {command.split()[0]}: command not found"

    def _is_directory(self, path):
        """Check if path is a directory in fake filesystem"""
        parts = [p for p in path.split('/') if p]
        current = self.fake_fs['/']['contents']
        
        for part in parts[:-1]:
            if part in current and current[part]['type'] == 'dir':
                current = current[part]['contents']
            else:
                return False
                
        last_part = parts[-1] if parts else ""
        return last_part in current and current[last_part]['type'] == 'dir'

    def _get_file_content(self, path):
        """Get content of fake files"""
        parts = [p for p in path.split('/') if p]
        current = self.fake_fs['/']['contents']
        
        for part in parts[:-1]:
            if part in current and current[part]['type'] == 'dir':
                current = current[part]['contents']
            else:
                return f"cat: {path}: No such file or directory"
                
        file = parts[-1] if parts else ""
        if file in current and current[file]['type'] == 'file':
            return current[file]['content']
        return f"cat: {path}: No such file or directory"

    def run(self):
        """Main shell interaction loop"""
        try:
            # Configure channel
            self.channel.set_combine_stderr(True)
            self.channel.settimeout(0.1)
            
            # Send welcome message with proper formatting
            welcome_msg = (
                f"Welcome to Ubuntu 18.04.5 LTS (GNU/Linux 4.15.0-112-generic x86_64)\r\n\r\n"
                f" * Documentation:  https://help.ubuntu.com\r\n"
                f" * Management:     https://landscape.canonical.com\r\n"
                f" * Support:        https://ubuntu.com/advantage\r\n\r\n"
                f"Last login: {time.strftime('%a %b %d %H:%M:%S %Y')} from {self.client_ip}\r\n"
            )
            self.send(welcome_msg)
            
            # Main shell loop
            while not self.channel.closed:
                self.show_prompt()
                self.current_cmd = ""
                self.cursor_pos = 0
                
                # Command input loop
                while True:
                    try:
                        r, _, _ = select.select([self.channel], [], [], 0.5)
                        if not r:
                            if self.channel.closed:
                                break
                            continue
                            
                        data = self.channel.recv(1)
                        if not data:
                            break
                            
                        # Handle special keys first
                        if self.handle_special_keys(data):
                            continue
                            
                        # Handle Enter key
                        if data in (b"\r", b"\n"):
                            self.send(self.CRLF)
                            break
                            
                        # Handle printable characters
                        if 32 <= ord(data) <= 126:
                            self.current_cmd = (
                                self.current_cmd[:self.cursor_pos] + 
                                data.decode() + 
                                self.current_cmd[self.cursor_pos:]
                            )
                            self.cursor_pos += 1
                            self.send(data)  # Echo character
                            
                    except socket.timeout:
                        continue
                    except Exception as e:
                        COMMANDS_LOGGER.error(f"Shell error: {e}")
                        break

                if self.channel.closed:
                    break
                    
                # Execute command
                if self.current_cmd.strip():
                    response = self.execute_command(self.current_cmd)
                    if response == "logout":
                        self.send("Connection closed by remote host.\r\n")
                        break
                    if response:
                        self.send(response + "\r\n")
                        
        except Exception as e:
            COMMANDS_LOGGER.error(f"Shell crashed: {e}")
        finally:
            try:
                if not self.channel.closed:
                    self.channel.close()
            except:
                pass

# ====================== SSH SERVER ======================
class SSHServer(paramiko.ServerInterface):
    def __init__(self, client_ip, username=None, password=None):
        self.client_ip = client_ip
        self.username = username
        self.password = password
        self.event = threading.Event()

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        CREDS_LOGGER.info(f"IP: {self.client_ip} | Username: {username} | Password: {password}")
        if self.username and self.password:
            if username == self.username and password == self.password:
                return paramiko.AUTH_SUCCESSFUL
            return paramiko.AUTH_FAILED
        return paramiko.AUTH_SUCCESSFUL

    def check_channel_shell_request(self, channel):
        self.event.set()
        return True

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        return True

# ====================== CONNECTION HANDLER ======================
def handle_client(client_sock, client_addr, username=None, password=None):
    """Handle incoming SSH connections"""
    client_ip = client_addr[0]
    print(f"New connection from {client_ip}")
    
    transport = None
    try:
        transport = paramiko.Transport(client_sock)
        transport.local_version = SSH_BANNER
        transport.add_server_key(HOST_KEY)
        
        server = SSHServer(client_ip, username, password)
        transport.start_server(server=server)
        
        channel = transport.accept(20)
        if channel is None:
            print(f"Channel negotiation failed for {client_ip}")
            return
            
        # Start realistic shell
        shell = RealisticShell(channel, client_ip)
        shell.run()
        
    except Exception as e:
        print(f"Error with {client_ip}: {e}")
    finally:
        try:
            if transport:
                transport.close()
        except:
            pass
        client_sock.close()
        print(f"Connection closed for {client_ip}")

# ====================== MAIN SERVER ======================
def start_honeypot(host="0.0.0.0", port=2222, username=None, password=None):
    """Start the SSH honeypot server"""
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.settimeout(1.0)
    
    try:
        # Ensure log directory exists
        CREDS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        COMMANDS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        
        server_sock.bind((host, port))
        server_sock.listen(100)
        print(f"Honeypot running on {host}:{port}")
        print("Press Ctrl+C to stop...")
        
        while True:
            try:
                client_sock, client_addr = server_sock.accept()
                threading.Thread(
                    target=handle_client,
                    args=(client_sock, client_addr, username, password),
                    daemon=True
                ).start()
            except socket.timeout:
                continue
            except KeyboardInterrupt:
                print("\nShutting down honeypot...")
                break
            except Exception as e:
                print(f"Accept error: {e}")
                continue
                
    except Exception as e:
        print(f"Server error: {e}")
    finally:
        server_sock.close()
        print("Honeypot stopped.")

if __name__ == "__main__":
    try:
        start_honeypot(port=2222, username="admin")
    except KeyboardInterrupt:
        print("\nHoneypot stopped by user")
        sys.exit(0)
