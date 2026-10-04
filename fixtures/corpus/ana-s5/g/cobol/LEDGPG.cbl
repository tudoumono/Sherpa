       IDENTIFICATION DIVISION.
       PROGRAM-ID. LEDGPG.
       PROCEDURE DIVISION.
           EXEC SQL
               SELECT ID INTO :WS-ID
                 FROM LEDGER
           END-EXEC.
           GOBACK.
