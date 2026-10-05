       IDENTIFICATION DIVISION.
       PROGRAM-ID. QUOTED.
       PROCEDURE DIVISION.
           EXEC SQL
               SELECT ID INTO :WS-ID
                 FROM "T.X"
           END-EXEC.
           GOBACK.
