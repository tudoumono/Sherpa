VERSION 5.00
Begin VB.Form frmMain
   Caption         =   "Main"
End
Attribute VB_Name = "frmMain"
Attribute VB_GlobalNameSpace = False
Attribute VB_Creatable = False
Attribute VB_PredeclaredId = True
Attribute VB_Exposed = False

Private Sub cmdOK_Click()
    Call modData.LoadOrders(1)
End Sub
