def subscriber_deletion_summary(conn, subscriber_id):
    """Return exact row counts affected by deleting one subscriber."""
    post_filter = "SELECT id FROM community_posts WHERE subscriber_id=?"
    return {
        "quiz_attempts": conn.execute(
            "SELECT COUNT(*) FROM quiz_attempts WHERE subscriber_id=?",
            (subscriber_id,),
        ).fetchone()[0],
        "attempt_answers": conn.execute(
            """SELECT COUNT(*) FROM attempt_answers aa
               JOIN quiz_attempts qa ON qa.id=aa.attempt_id
               WHERE qa.subscriber_id=?""",
            (subscriber_id,),
        ).fetchone()[0],
        "participation": conn.execute(
            "SELECT COUNT(*) FROM participation WHERE subscriber_id=?",
            (subscriber_id,),
        ).fetchone()[0],
        "legacy_participation": conn.execute(
            "SELECT COUNT(*) FROM legacy_participation WHERE subscriber_id=?",
            (subscriber_id,),
        ).fetchone()[0],
        "feedback_submissions": conn.execute(
            "SELECT COUNT(*) FROM feedback_submissions WHERE subscriber_id=?",
            (subscriber_id,),
        ).fetchone()[0],
        "feedback_answers": conn.execute(
            """SELECT COUNT(*) FROM feedback_answers fa
               JOIN feedback_submissions fs ON fs.id=fa.submission_id
               WHERE fs.subscriber_id=?""",
            (subscriber_id,),
        ).fetchone()[0],
        "magic_link_tokens": conn.execute(
            "SELECT COUNT(*) FROM magic_link_tokens WHERE subscriber_id=?",
            (subscriber_id,),
        ).fetchone()[0],
        "community_posts": conn.execute(
            "SELECT COUNT(*) FROM community_posts WHERE subscriber_id=?",
            (subscriber_id,),
        ).fetchone()[0],
        "community_comments": conn.execute(
            f"""SELECT COUNT(*) FROM community_comments
                WHERE subscriber_id=? OR post_id IN ({post_filter})""",
            (subscriber_id, subscriber_id),
        ).fetchone()[0],
        "community_likes": conn.execute(
            f"""SELECT COUNT(*) FROM community_likes
                WHERE subscriber_id=? OR post_id IN ({post_filter})""",
            (subscriber_id, subscriber_id),
        ).fetchone()[0],
        "linked_registrations": conn.execute(
            "SELECT COUNT(*) FROM subscription_registrations WHERE subscriber_id=?",
            (subscriber_id,),
        ).fetchone()[0],
        "linked_dog_profiles": conn.execute(
            "SELECT COUNT(*) FROM dog_profiles WHERE subscriber_id=?",
            (subscriber_id,),
        ).fetchone()[0],
    }


def delete_subscriber_data(conn, subscriber_id):
    """Delete subscriber-owned records inside the caller's transaction.

    Subscription registrations and dog profiles are retained. Their foreign
    keys use ON DELETE SET NULL and are detached when the subscriber row is
    deleted.
    """
    exists = conn.execute(
        "SELECT 1 FROM subscribers WHERE id=?", (subscriber_id,)
    ).fetchone()
    if not exists:
        return False

    # Remove user-authored interactions first. Deleting owned posts then
    # cascades comments and likes that other subscribers left on those posts.
    conn.execute(
        "DELETE FROM community_comments WHERE subscriber_id=?", (subscriber_id,)
    )
    conn.execute(
        "DELETE FROM community_likes WHERE subscriber_id=?", (subscriber_id,)
    )
    conn.execute(
        "DELETE FROM community_posts WHERE subscriber_id=?", (subscriber_id,)
    )

    # Child answer rows cascade from their parent submissions/attempts.
    conn.execute(
        "DELETE FROM feedback_submissions WHERE subscriber_id=?", (subscriber_id,)
    )
    # participation.first_attempt_id prevents deleting attempts first.
    conn.execute(
        "DELETE FROM participation WHERE subscriber_id=?", (subscriber_id,)
    )
    conn.execute(
        "DELETE FROM quiz_attempts WHERE subscriber_id=?", (subscriber_id,)
    )
    conn.execute(
        "DELETE FROM legacy_participation WHERE subscriber_id=?", (subscriber_id,)
    )
    conn.execute(
        "DELETE FROM magic_link_tokens WHERE subscriber_id=?", (subscriber_id,)
    )
    conn.execute("DELETE FROM subscribers WHERE id=?", (subscriber_id,))
    return True
