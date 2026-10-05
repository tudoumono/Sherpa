Namespace Acme.Shop
    Public Class Cart
        Inherits Basket
        Public Sub Add()
            Dim h As Helper
            Recalc()
        End Sub
        Public Sub Recalc()
            Dim p As Price
        End Sub
    End Class

    Class Helper
        Public Sub Assist()
            Dim q As Price
        End Sub
    End Class
End Namespace
