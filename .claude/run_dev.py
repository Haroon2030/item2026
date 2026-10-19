import os, sys
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(root)
sys.path.insert(0, root)
port = os.environ.get("PORT", "8000")
sys.argv = ["manage.py", "runserver", port, "--noreload"]
exec(compile(open("manage.py", encoding="utf-8").read(), "manage.py", "exec"), {"__name__": "__main__", "__file__": os.path.join(root, "manage.py")})
