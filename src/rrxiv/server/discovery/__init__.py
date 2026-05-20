"""Discovery endpoints — instance metadata for UI browse + search.

Endpoints in this router (``/scopes``, ``/topics``) are *instance metadata*,
not protocol-binding. Other rrxiv instances may publish different scopes
and a different topic vocabulary. Clients read what the server publishes
rather than hardcoding.
"""
