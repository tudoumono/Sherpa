using A = Lib;
using B = GlobalT;

namespace AliasNs
{
    public class Target
    {
    }

    public class Use
    {
        private A.Target viaAlias;
        private B viaSimple;
    }
}
