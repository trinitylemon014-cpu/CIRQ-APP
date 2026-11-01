from app.services import supabase_service as sb


def audit(actor_id, action, target_type=None, target_id=None, **meta):
    """Append-only administrative audit record. Never put secrets in meta."""
    sb.insert("audit_logs", {"actor_id": actor_id, "action": action, "target_type": target_type,
                             "target_id": str(target_id) if target_id else None, "metadata": meta})
