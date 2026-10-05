       IDENTIFICATION DIVISION.
       PROGRAM-ID. QUALC.
       PROCEDURE DIVISION.
           EXEC SQL
               SELECT ID INTO :WS-ID
                 FROM C.CUSTOMER
           END-EXEC.
           GOBACK.
