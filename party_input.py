import sys
from utils.os_detector import IS_WINDOWS

class NonBlockingInput:
    def __init__(self):
        self.is_windows = IS_WINDOWS
        self.fd = None
        self.old_settings = None
        
        if not self.is_windows:
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.old_settings = termios.tcgetattr(self.fd)
            # cbreak means rawish but ctrl+c still works
            tty.setcbreak(self.fd)

    def cleanup(self):
        if not self.is_windows and self.fd is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_settings)

    def get_char(self):
        if self.is_windows:
            import msvcrt
            if msvcrt.kbhit():
                c = msvcrt.getwch()
                if c == '\r':
                    return '\n'
                # Handle special keys on Windows (PageUp/Down)
                if c == '\x00' or c == '\xe0':
                    # It's a special key prefix
                    c2 = msvcrt.getwch()
                    if c2 == 'I': return 'PAGEUP'
                    if c2 == 'Q': return 'PAGEDOWN'
                return c
            return None
        else:
            import select
            dr, _, _ = select.select([sys.stdin], [], [], 0)
            if dr:
                c = sys.stdin.read(1)
                # Handle Escape sequences (Linux/macOS)
                if c == '\x1b':
                    dr2, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if dr2:
                        c2 = sys.stdin.read(2)
                        if c2 == '[5': # PageUp part 1
                            sys.stdin.read(1) # consume ~
                            return 'PAGEUP'
                        if c2 == '[6': # PageDown part 1
                            sys.stdin.read(1) # consume ~
                            return 'PAGEDOWN'
                        return '\x1b' + c2
                if c == '\x7f':
                    return '\x08' # normalize backspace
                return c
            return None
