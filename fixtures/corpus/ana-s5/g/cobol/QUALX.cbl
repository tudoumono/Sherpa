       IDENTIFICATION DIVISION.
       PROGRAM-ID. QUALX.
       PROCEDURE DIVISION.
           EXEC SQL
               SELECT ID INTO :WS-ID
                 FROM P.X
           END-EXEC.
           GOBACK.
