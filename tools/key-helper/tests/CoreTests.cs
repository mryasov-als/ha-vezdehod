using System;
using System.Collections.Generic;
using System.Threading;
using VezdehodKeyHelper;

internal static class CoreTests
{
    private const string Pair = "auth_phone=79990001122&mobile_key=fixture-key-only-123";
    private static int passed;

    private static void Check(bool condition, string name)
    {
        if (!condition) throw new Exception("FAIL: " + name);
        passed++;
    }

    private static string Line(string url, string time)
    {
        return time + "  1234  1234 I okhttp.OkHttpClient: --> GET " + url;
    }

    public static int Main()
    {
        try
        {
            string baseUrl = "https://lk.comendant24.ru/vezdehod/v1/get_facility?";
            Credentials found = CredentialParser.Parse(Line(baseUrl + Pair, "1800000002.25"), 1800000001m);
            Check(found != null, "fresh outgoing request");
            Check(found.Phone == "79990001122", "phone decoded");
            Check(found.Key == "fixture-key-only-123", "key preserved");
            Check(!found.ToString().Contains(found.Key), "no key in ToString");
            Check(!found.ToString().Contains(found.Phone), "no phone in ToString");
            string[] rejected = {
                "http://lk.comendant24.ru/vezdehod/v1/get_facility?" + Pair,
                "https://lk.comendant24.ru.evil.invalid/vezdehod/v1/get_facility?" + Pair,
                "https://evil.invalid/vezdehod/v1/get_facility?" + Pair,
                "https://lk.comendant24.ru:444/vezdehod/v1/get_facility?" + Pair,
                "https://user@lk.comendant24.ru/vezdehod/v1/get_facility?" + Pair,
                "https://lk.comendant24.ru/vezdehod/v1/open?" + Pair,
                baseUrl + Pair + "&mobile_key=other-fixture-key",
                baseUrl + Pair + "&auth_phone=79990003344",
                baseUrl + "auth_phone=79990001122",
                baseUrl + "mobile_key=fixture-key-only-123",
                baseUrl + "auth_phone=wrong&mobile_key=fixture-key-only-123",
                baseUrl + "auth_phone=79990001122&mobile_key=bad%0Akey",
                baseUrl + "auth_phone=79990001122&mobile_key=fixture%0Akey-only-123",
                baseUrl + "auth_phone=79990001122&mobile_key=fixture%ZZ-key-only-123",
                baseUrl + "auth_phone=79990001122&mobile_key=",
                baseUrl + Pair + "&%6dobile_key=other-fixture-key",
                baseUrl + Pair + "#fragment"
            };
            int rejectedCase = 0;
            foreach (string value in rejected)
                Check(CredentialParser.Parse(Line(value, "1800000002.25"), 1800000001m) == null,
                    "reject unsafe or ambiguous URL case " + rejectedCase++);
            Check(CredentialParser.Parse(Line(baseUrl + Pair, "1799999999.99"), 1800000001m) == null,
                "reject stale log");
            Check(CredentialParser.Parse("--> GET " + baseUrl + Pair, 0m) == null,
                "require log timestamp");
            Check(CredentialParser.Parse(Line(baseUrl + Pair, "1800000002").Replace("--> GET", "<-- 200"), 0m) == null,
                "ignore response URL");
            Check(CredentialParser.Parse(Line(baseUrl + Pair, "1800000002").Replace("--> GET", "--> POST"), 0m) == null,
                "ignore mutating request");
            Check(CredentialParser.Parse("1800000002 1234 1234 I okhttp.OkHttpClient: {\"description\":\"--> GET " +
                baseUrl + Pair + "\"}", 0m) == null, "ignore request-looking text inside response body");
            Check(CredentialParser.Parse(Line(baseUrl + Pair, "1800000002").Replace("okhttp.OkHttpClient", "OtherApp"), 0m) == null,
                "require exact logger tag");
            found = CredentialParser.Parse(Line("https://client.comendant24.ru/barrier/mobileapp/check_auth?" +
                "auth_phone=%2B79990001122&mobile_key=fixture%2Dkey%2Donly%2D123", "1800000002"), 0m);
            Check(found != null && found.Phone == "79990001122" && found.Key == "fixture-key-only-123",
                "legacy endpoint and URL decoding");
            Check(CredentialParser.Parse(new string('x', 40000), 0m) == null, "bounded input");

            FakeRunner fake = new FakeRunner(baseUrl + Pair);
            CaptureEngine engine = new CaptureEngine(fake, delegate(int ms, CancellationToken token) { token.ThrowIfCancellationRequested(); });
            List<ReaderStatus> statuses = new List<ReaderStatus>();
            found = engine.Capture(CancellationToken.None, statuses.Add);
            Check(found != null, "capture found");
            Check(fake.LogReads == 1, "stops immediately after first match");
            Check(statuses.Exists(delegate(ReaderStatus status) { return status.Kind == StatusKind.WaitingForApp; }),
                "guides user to app");
            Check(fake.Commands.TrueForAll(delegate(string command) {
                return command.StartsWith("-d ", StringComparison.Ordinal);
            }), "only USB transport");
            Check(!fake.Commands.Exists(delegate(string command) {
                return command.Contains("kill-server") || command.Contains(" logcat -c") || command.Contains(" am ") || command.Contains("auth_phone");
            }), "no global changes or network credentials in commands");

            CancellationTokenSource cancel = new CancellationTokenSource();
            fake = new FakeRunner(baseUrl + Pair) { Authorized = false };
            engine = new CaptureEngine(fake, delegate(int ms, CancellationToken token) { cancel.Cancel(); token.ThrowIfCancellationRequested(); });
            bool cancelled = false;
            try { engine.Capture(cancel.Token, delegate(ReaderStatus status) { }); }
            catch (OperationCanceledException) { cancelled = true; }
            Check(cancelled && fake.LogReads == 0, "stop cancels while waiting for authorization");

            fake = new FakeRunner(baseUrl + Pair) { Installed = false };
            engine = new CaptureEngine(fake, delegate(int ms, CancellationToken token) { });
            bool missing = false;
            try { engine.Capture(CancellationToken.None, delegate(ReaderStatus status) { }); }
            catch (ReaderException ex) { missing = ex.Code == "app_missing"; }
            Check(missing && fake.LogReads == 0, "missing app is explained without reading logs");

            fake = new FakeRunner(baseUrl + Pair) { ManyProcesses = true };
            engine = new CaptureEngine(fake, delegate(int ms, CancellationToken token) { });
            bool ambiguousProcess = false;
            try { engine.Capture(CancellationToken.None, delegate(ReaderStatus status) { }); }
            catch (ReaderException ex) { ambiguousProcess = ex.Code == "multiple_app_processes"; }
            Check(ambiguousProcess && fake.LogReads == 0, "refuses multiple app profiles");

            fake = new FakeRunner(baseUrl + Pair) { FailLog = true };
            engine = new CaptureEngine(fake, delegate(int ms, CancellationToken token) { });
            bool safeError = false;
            try { engine.Capture(CancellationToken.None, delegate(ReaderStatus status) { }); }
            catch (ReaderException ex) { safeError = ex.Code == "log_unavailable" && !ex.ToString().Contains("fixture-key-only-123") && !ex.Message.Contains("79990001122"); }
            Check(safeError && fake.LogReads == 1, "never displays raw log errors with credentials");

            fake = new FakeRunner(baseUrl + Pair);
            engine = new CaptureEngine(fake, delegate(int ms, CancellationToken token) { });
            cancel = new CancellationTokenSource(); cancel.Cancel();
            cancelled = false;
            try { engine.Capture(cancel.Token, delegate(ReaderStatus status) { }); }
            catch (OperationCanceledException) { cancelled = true; }
            Check(cancelled && fake.Commands.Count == 0, "cancelled capture launches no commands");
            Console.WriteLine("PASS: " + passed + " checks; no devices accessed");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.Message);
            return 1;
        }
    }

    private sealed class FakeRunner : IAdbRunner
    {
        public bool Authorized = true;
        public bool Installed = true;
        public bool ManyProcesses;
        public bool FailLog;
        public int LogReads;
        public readonly List<string> Commands = new List<string>();
        private readonly string url;
        public FakeRunner(string value) { url = value; }
        public CommandResult Run(string arguments, CancellationToken token, int timeoutMs)
        {
            token.ThrowIfCancellationRequested();
            Commands.Add(arguments);
            if (arguments == "-d get-state") return Authorized ? new CommandResult(0, "device", "") : new CommandResult(1, "", "device unauthorized");
            if (arguments.Contains("pm path")) return new CommandResult(0, Installed ? "package:/data/app/example/base.apk" : "", "");
            if (arguments.Contains("date +%s")) return new CommandResult(0, "1800000000", "");
            if (arguments.Contains("pidof")) return new CommandResult(0, ManyProcesses ? "1234 5678" : "1234", "");
            if (arguments.Contains("logcat"))
            {
                LogReads++;
                if (FailLog) return new CommandResult(1, "", url);
                return new CommandResult(0,
                    Line(url.Replace("fixture-key-only-123", "stale-fixture-key"), "1799999999.2") + "\n" +
                    Line(url, "1800000002.5"), "");
            }
            throw new Exception("Unexpected test command");
        }
    }
}
