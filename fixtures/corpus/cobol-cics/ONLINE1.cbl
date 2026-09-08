       IDENTIFICATION DIVISION.
       PROGRAM-ID. ONLINE1.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-NEXT              PIC X(8).
       01  WS-AREA              PIC X(80).
       PROCEDURE DIVISION.
           EXEC CICS
               XCTL PROGRAM('MENU01')
           END-EXEC.
           EXEC CICS
               LINK PROGRAM('SUBR01') COMMAREA(WS-AREA)
           END-EXEC.
           EXEC CICS
               XCTL PROGRAM(WS-NEXT)
           END-EXEC.
           EXEC CICS
               SEND MAP('M1')
           END-EXEC.
           EXEC CICS
               LINK PROGRAM('SUB
      -    'R02')
           END-EXEC.
           EXEC CICS
               LINK PROGRAM('NOPE')
           END-EXEC.
           GOBACK.
