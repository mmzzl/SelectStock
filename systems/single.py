import os
import sys

if sys.platform == 'win32':
    import msvcrt

    def flock_exclusive(filepath):
        """Windows file lock using msvcrt"""
        file_dir = os.path.dirname(filepath)
        if not os.path.exists(file_dir):
            os.makedirs(file_dir)
        pidfile = open(filepath, 'wb')
        try:
            msvcrt.locking(pidfile.fileno(), msvcrt.LK_NBLCK, 1)
            return pidfile, None
        except (IOError, OSError):
            pidfile.close()
            return None, 'locked'

    def flock_unlock(pidfile):
        """Unlock Windows file"""
        if pidfile:
            try:
                msvcrt.locking(pidfile.fileno(), msvcrt.LK_UNLCK, 1)
                pidfile.close()
            except:
                pass
else:
    import fcntl

    def flock_exclusive(filepath):
        """Linux file lock using fcntl"""
        pidfile = open(filepath, 'w')
        try:
            fcntl.flock(pidfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return pidfile, None
        except (IOError, OSError):
            pidfile.close()
            return None, 'locked'

    def flock_unlock(pidfile):
        """Unlock Linux file"""
        if pidfile:
            try:
                fcntl.flock(pidfile.fileno(), fcntl.LOCK_UN)
                pidfile.close()
            except:
                pass


class ScriptSingle:
    pidfile = 0
    is_run = False

    def __init__(self, filepath):
        result, error = flock_exclusive(filepath)
        if error:
            self.is_run = True
            self.pidfile = None
        else:
            self.pidfile = result

    def is_running(self):
        return self.is_run


class ClassSingle(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(ClassSingle, cls).__call__(*args, **kwargs)
        return cls._instances[cls]