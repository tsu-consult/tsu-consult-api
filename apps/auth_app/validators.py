import re


def validate_human_name(value: str, field_name: str = "first_name") -> None:
    value = value.strip()

    if len(value) < 2 or len(value) > 50:
        raise ValueError(f"{field_name.capitalize()} must contain from 2 to 50 characters.")

    if re.search(r'[А-Яа-яЁё]', value) and re.search(r'[A-Za-z]', value):
        raise ValueError(f"{field_name.capitalize()} should not contain mixed alphabets.")

    if not re.match(r"^[A-Za-zÀ-ÿ'’\-А-Яа-яЁё\s]+$", value):
        raise ValueError(f"{field_name.capitalize()} contains invalid characters.")

    if re.fullmatch(r'(test|asd|qwe|имя|name|none|unknown)', value, re.IGNORECASE):
        raise ValueError(f"{field_name.capitalize()} looks unrealistic.")
