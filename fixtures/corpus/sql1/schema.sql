-- schema.sql（アナライザ拡張 S2・fixtures/corpus/sql1）
-- コメント内の罠: CREATE TABLE FAKE_IN_COMMENT (X INT);

CREATE TABLE orders (
    id INT PRIMARY KEY,
    customer_id INT NOT NULL,
    memo VARCHAR(255) DEFAULT 'trap: CREATE TABLE FAKE_IN_STRING (Y INT)',
    CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE "ORDER_LINES" (
    order_id INT NOT NULL,
    line_no INT NOT NULL,
    qty INT,
    PRIMARY KEY (order_id, line_no)
);

/* 複数行コメントの罠: CREATE TABLE FAKE_IN_BLOCK_COMMENT (Z INT); */
CREATE TABLE customers (
    id INT PRIMARY KEY,
    name VARCHAR(100),
    KEY idx_customers_name (name)
);

CREATE VIEW active_orders AS
    SELECT * FROM orders WHERE status = 'NEW';

ALTER TABLE orders ADD COLUMN notes VARCHAR(255);
