-- FILE NÀY ĐƯỢC SINH TỰ ĐỘNG từ app/db/models.py
-- Sửa model rồi chạy: python -m scripts.dump_schema
-- Không sửa tay file này.


CREATE TABLE IF NOT EXISTS chat_sessions (
	id UUID NOT NULL, 
	user_id BIGINT NOT NULL, 
	summary TEXT, 
	summarized_through_id BIGINT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_created ON chat_sessions (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
	id BIGSERIAL NOT NULL, 
	session_id UUID NOT NULL, 
	user_id BIGINT NOT NULL, 
	request_id UUID NOT NULL, 
	role VARCHAR(16) NOT NULL, 
	content TEXT NOT NULL, 
	meta JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_chat_messages_role CHECK (role IN ('user', 'assistant')), 
	FOREIGN KEY(session_id) REFERENCES chat_sessions (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id ON chat_messages (session_id, id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_messages_request_role ON chat_messages (request_id, role);
