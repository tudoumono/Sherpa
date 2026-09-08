       IDENTIFICATION DIVISION.
       PROGRAM-ID. BILLING.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-COL1              PIC X(10).
       01  WS-COL2              PIC X(10).
       01  WS-ORDER-ID          PIC 9(6).
       01  WS-QTY               PIC 9(4).
       01  WS-ID                PIC 9(6).
       PROCEDURE DIVISION.
           EXEC SQL
               SELECT COL1, COL2
                 INTO :WS-COL1, :WS-COL2
                 FROM ORDERS
           END-EXEC.
           EXEC SQL
               INSERT INTO ORDER_LINES (ORDER_ID, QTY)
               VALUES (:WS-ORDER-ID, :WS-QTY)
           END-EXEC.
           EXEC SQL
               UPDATE customers
               SET STATUS = 'X'
               WHERE ID = :WS-ID
           END-EXEC.
           EXEC SQL
               SELECT O.ID, C.NAME, S.CARRIER
                 FROM ORDERS O
                 JOIN CUSTOMERS C
                   ON O.CUST_ID = C.ID
                 JOIN SHIPPING_INFO S
                   ON O.ID = S.ORDER_ID
           END-EXEC.
           EXEC SQL
               DECLARE CUR1 CURSOR FOR
               SELECT * FROM ORDERS
           END-EXEC.
           GOBACK.
