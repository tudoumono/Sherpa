namespace Raw;

#region {
public class Holder
{
    private const string S = """
        "quoted" { class Fake {} }
        """;
    private string t = $$"""x {{1}} " class Fake2 { """;
    private Dep d;
}
#endregion

public class After
{
    private Dep e;
}
