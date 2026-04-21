"""Dev helper — print a signed JWT for manual testing.

Usage:
  JWT_SECRET=my-secret python issue_token.py [subject] [ttl_hours]
"""
import sys
import time
import jwt
import os

sub = sys.argv[1] if len(sys.argv) > 1 else "dev-user"
ttl = int(sys.argv[2]) * 3600 if len(sys.argv) > 2 else 86400
secret = os.getenv("JWT_SECRET", "change-me-in-production")

token = jwt.encode(
    {"sub": sub, "exp": int(time.time()) + ttl},
    secret,
    algorithm="HS256",
)
print(token)
