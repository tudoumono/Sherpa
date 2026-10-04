       IDENTIFICATION DIVISION.
       PROGRAM-ID. PLAINPG.
       PROCEDURE DIVISION.
           EXEC SQL
               SELECT ID INTO :WS-ID
                 FROM CUSTOMER
           END-EXEC.
           GOBACK.
