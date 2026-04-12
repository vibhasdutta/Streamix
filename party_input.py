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
                return c
            return None
        else:
            import select
            dr, _, _ = select.select([sys.stdin], [], [], 0)
            if dr:
                c = sys.stdin.read(1)
                if c == '\x7f':
                    return '\x08' # normalize backspace
                return c
            return None
