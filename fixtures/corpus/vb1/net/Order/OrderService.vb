Namespace Acme.Order

    Public Class OrderService
        Inherits BaseService

        Private ReadOnly Name As String

        Public Sub New()
            Name = "orders"
        End Sub

        Public Sub Process()
            LogStart()
        End Sub

        Private Sub LogStart()
        End Sub
    End Class

End Namespace
