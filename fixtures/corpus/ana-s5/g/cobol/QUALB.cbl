       IDENTIFICATION DIVISION.
       PROGRAM-ID. QUALB.
       PROCEDURE DIVISION.
           EXEC SQL
               SELECT ID INTO :WS-ID
                 FROM B.CUSTOMER
           END-EXEC.
           GOBACK.
