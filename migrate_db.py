import sqlite3

db_path = "sql_app.db"

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check if column exists
    cursor.execute("PRAGMA table_info(biddings)")
    columns = [info[1] for info in cursor.fetchall()]
    
    if "notice_type" not in columns:
        print("Adding notice_type column...")
        cursor.execute("ALTER TABLE biddings ADD COLUMN notice_type TEXT DEFAULT '中标公告'")
        conn.commit()
        print("Column added successfully.")
    else:
        print("Column notice_type already exists.")
    
    # Add opportunity_analysis column
    if "opportunity_analysis" not in columns:
        print("Adding opportunity_analysis column...")
        cursor.execute("ALTER TABLE biddings ADD COLUMN opportunity_analysis TEXT DEFAULT ''")
        conn.commit()
        print("Column opportunity_analysis added successfully.")
    else:
        print("Column opportunity_analysis already exists.")
        
    conn.close()
    print("\n数据库迁移完成！")
except Exception as e:
    print(f"Error: {e}")
