CREATE TABLE ORDERS (
    id INT PRIMARY KEY,
    status VARCHAR(20)
);

CREATE TABLE ORDER_LINES (
    id INT PRIMARY KEY,
    order_id INT NOT NULL,
    qty INT,
    CONSTRAINT fk_order FOREIGN KEY (order_id) REFERENCES ORDERS(id)
);

CREATE TABLE "customers" (
    id INT PRIMARY KEY,
    name VARCHAR(100)
);
