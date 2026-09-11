"""
Sinh `db/schema.sql` từ model SQLAlchemy.

    python -m scripts.dump_schema

Chạy lại mỗi khi sửa `app/db/models.py` và commit kèm. Không sửa tay schema.sql:
model là nguồn sự thật duy nhất.

Đây chưa phải migration tool — Phase 6 (rollout) nên chuyển sang Alembic khi
schema bắt đầu phải đổi trên dữ liệu thật.
"""

from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.db.models import Base

OUTPUT = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

HEADER = """-- FILE NÀY ĐƯỢC SINH TỰ ĐỘNG từ app/db/models.py
-- Sửa model rồi chạy: python -m scripts.dump_schema
-- Không sửa tay file này.

"""


def render() -> str:
    dialect = postgresql.dialect()
    parts = [HEADER]
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
        parts.append(ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1) + ";\n")
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            idx = str(CreateIndex(index).compile(dialect=dialect)).strip()
            parts.append(idx.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1)
                         .replace("CREATE UNIQUE INDEX", "CREATE UNIQUE INDEX IF NOT EXISTS", 1) + ";\n")
    return "\n".join(parts)


if __name__ == "__main__":
    OUTPUT.write_text(render(), encoding="utf-8")
    print(f"Đã ghi {OUTPUT}")
