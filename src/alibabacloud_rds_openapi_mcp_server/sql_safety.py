import re


class SqlSafetyValidator:
    MAX_SQL_LENGTH = 10000

    _IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_$]+$")
    _LEADING_KEYWORD_PATTERN = re.compile(r"^\s*([A-Za-z]+)")
    _DANGEROUS_PATTERN = re.compile(
        r"\b("
        r"alter|call|copy|create|delete|drop|execute|exec|grant|insert|kill|load|"
        r"lock|merge|optimize|repair|replace|revoke|set|truncate|unlock|update|use"
        r")\b",
        re.IGNORECASE,
    )

    @classmethod
    def quote_identifier(cls, identifier: str, field_name: str) -> str:
        if not isinstance(identifier, str) or not cls._IDENTIFIER_PATTERN.fullmatch(identifier):
            raise ValueError(f"Invalid {field_name}. Use only letters, numbers, underscores, '$'.")
        return f"`{identifier}`"

    @classmethod
    def validate_read_only_sql(cls, sql: str) -> str:
        stripped_sql = cls._validate_single_statement(sql)
        leading_keyword = cls._get_leading_keyword(stripped_sql)
        if leading_keyword not in ("select", "show", "describe", "desc", "explain"):
            raise ValueError("Only read-only SQL statements are allowed.")
        if leading_keyword == "select" and cls._DANGEROUS_PATTERN.search(stripped_sql):
            raise ValueError("Only read-only SQL statements are allowed.")
        return stripped_sql

    @classmethod
    def validate_explain_sql(cls, sql: str) -> str:
        stripped_sql = cls._validate_single_statement(sql)
        if cls._get_leading_keyword(stripped_sql) != "select":
            raise ValueError("EXPLAIN only supports SELECT statements.")
        if cls._DANGEROUS_PATTERN.search(stripped_sql):
            raise ValueError("EXPLAIN only supports SELECT statements.")
        return stripped_sql

    @classmethod
    def _validate_single_statement(cls, sql: str) -> str:
        if not isinstance(sql, str):
            raise ValueError("SQL must be a string.")
        stripped_sql = sql.strip()
        if not stripped_sql:
            raise ValueError("SQL must not be empty.")
        if len(stripped_sql) > cls.MAX_SQL_LENGTH:
            raise ValueError("SQL is too long.")
        if ";" in stripped_sql or "--" in stripped_sql or "#" in stripped_sql or "/*" in stripped_sql:
            raise ValueError("SQL must be a single read-only statement without comments.")
        return stripped_sql

    @classmethod
    def _get_leading_keyword(cls, sql: str) -> str:
        match = cls._LEADING_KEYWORD_PATTERN.match(sql)
        if not match:
            raise ValueError("SQL must start with a supported read-only keyword.")
        return match.group(1).lower()
