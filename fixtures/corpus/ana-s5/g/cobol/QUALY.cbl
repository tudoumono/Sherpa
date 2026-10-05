       IDENTIFICATION DIVISION.
       PROGRAM-ID. QUALY.
       PROCEDURE DIVISION.
           EXEC SQL
               SELECT ID INTO :WS-ID
                 FROM P.Y
           END-EXEC.
           GOBACK.
