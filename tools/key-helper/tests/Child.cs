using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Threading;

internal static class Child
{
    public static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "-L") return SimulateAdb(args);
        if (args.Length > 0 && args[0] == "slow")
        {
            if (args.Length > 1)
            {
                File.WriteAllText(args[1] + ".tmp", Process.GetCurrentProcess().Id.ToString());
                File.Move(args[1] + ".tmp", args[1]);
            }
            Thread.Sleep(10000);
        }
        if (args.Length > 0 && args[0] == "flood")
            for (int i = 0; i < 1500; i++) Console.WriteLine(new string('x', 4096));
        Console.WriteLine("fixture-output");
        Console.Error.WriteLine("fixture-error");
        return 23;
    }

    private static int SimulateAdb(string[] args)
    {
        if (Environment.GetEnvironmentVariable("ADB_SERVER_SOCKET") != null ||
            Environment.GetEnvironmentVariable("ANDROID_ADB_SERVER_ADDRESS") != null ||
            Environment.GetEnvironmentVariable("ANDROID_ADB_SERVER_PORT") != null)
            return 2;

        int port;
        if (args.Length == 4 && args[1].StartsWith("tcp:localhost:") &&
            Int32.TryParse(args[1].Substring("tcp:localhost:".Length), out port) &&
            args[2] == "server" && args[3] == "nodaemon")
        {
            if (Environment.GetEnvironmentVariable("ADB_USB") == "0" ||
                Environment.GetEnvironmentVariable("ADB_MDNS") != "0" ||
                Environment.GetEnvironmentVariable("ADB_EMU") != "0") return 2;
            TcpListener listener = new TcpListener(IPAddress.Loopback, port);
            listener.ExclusiveAddressUse = true;
            listener.Start();
            while (true)
            {
                using (TcpClient connection = listener.AcceptTcpClient())
                using (StreamWriter writer = new StreamWriter(connection.GetStream()))
                    writer.WriteLine(Process.GetCurrentProcess().Id);
            }
        }
        if (args.Length == 3 && args[1].StartsWith("tcp:127.0.0.1:") &&
            Int32.TryParse(args[1].Substring("tcp:127.0.0.1:".Length), out port) &&
            args[2] == "fixture")
        {
            using (TcpClient client = new TcpClient("127.0.0.1", port))
            using (StreamReader reader = new StreamReader(client.GetStream()))
                Console.WriteLine(reader.ReadLine());
            return 0;
        }
        return 2;
    }
}
