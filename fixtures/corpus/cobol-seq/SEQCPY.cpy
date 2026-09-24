000010* 採番付きコピーブック（S1・delevel 確認）
000020     01  SEQ-REC.
000030         05  SEQ-AMT       PIC 9(5) VALUE 100.
000040         05  SEQ-FILLER-CHK.
000050             10  FILLER        PIC X(3).
000060             10  SEQ-SUB-ITEM  PIC X(2).
000070         66  SEQ-RENAME    RENAMES SEQ-AMT.
000080         88  SEQ-FLAG      VALUE 'Y'.
