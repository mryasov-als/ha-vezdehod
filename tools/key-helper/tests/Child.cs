using System;
using System.Diagnostics;
using System.IO;
using System.Threading;

internal static class Child
{
    public static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "slow")
        {
            if (args.Length > 1) File.WriteAllText(args[1], Process.GetCurrentProcess().Id.ToString());
            Thread.Sleep(10000);
        }
        if (args.Length > 0 && args[0] == "flood")
            for (int i = 0; i < 1500; i++) Console.WriteLine(new string('x', 4096));
        Console.WriteLine("fixture-output");
        Console.Error.WriteLine("fixture-error");
        return 23;
    }
}
