import os
import pwd
import sys


def chown_tree(path: str, uid: int, gid: int) -> None:
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)
    for root, dirs, files in os.walk(path):
        os.chown(root, uid, gid)
        for name in dirs:
            os.chown(os.path.join(root, name), uid, gid)
        for name in files:
            os.chown(os.path.join(root, name), uid, gid)


def main() -> None:
    app_user = pwd.getpwnam("appuser")
    chown_tree("/data", app_user.pw_uid, app_user.pw_gid)
    os.setgid(app_user.pw_gid)
    os.setuid(app_user.pw_uid)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
