def titlecase_area(s):
    """Like str.title(), but doesn't capitalize the letter right after an
    apostrophe (str.title() turns "ST JAMES'S" into "St James'S")."""
    words = []
    for w in s.split(" "):
        if "'" in w and len(w) > 1:
            words.append(w[0].upper() + w[1:].lower())
        else:
            words.append(w.capitalize())
    return " ".join(words)
