using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("Real-Time Subtitle Translator")]
[assembly: AssemblyProduct("Real-Time Subtitle Translator")]

internal static class Launcher
{
    private const string Title = "实时字幕翻译";
    private const string Ready = "REALTIME_SUBTITLE_RUNTIME_READY";

    [STAThread]
    private static int Main(string[] args)
    {
        bool interactive = Environment.GetEnvironmentVariable("REALTIME_SUBTITLE_NO_PAUSE") != "1";
        try
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string script = Path.Combine(root, "run.ps1");
            if (!File.Exists(script) || !File.Exists(Path.Combine(root, "main.py")))
                throw new FileNotFoundException("请将 EXE 和解压后的项目文件放在同一目录。");

            string powershell = Path.Combine(Environment.SystemDirectory,
                @"WindowsPowerShell\v1.0\powershell.exe");
            var arguments = new StringBuilder("-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ");
            arguments.Append(Quote(script));
            var pythonArguments = new StringBuilder(Quote(Path.Combine(root, "main.py")));
            foreach (string arg in args)
                pythonArguments.Append(' ').Append(Quote(arg));

            var start = new ProcessStartInfo(powershell, arguments.ToString())
            {
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };
            start.EnvironmentVariables["REALTIME_SUBTITLE_GUI_LAUNCH"] = "1";
            // Bypass PowerShell 5.1's native-argument rewriting on the Python hop.
            start.EnvironmentVariables["REALTIME_SUBTITLE_PYTHON_ARGS"] = pythonArguments.ToString();
            return Launch(start, interactive);
        }
        catch (Exception error)
        {
            ShowError(error.Message, interactive);
            return 1;
        }
    }

    private static int Launch(ProcessStartInfo start, bool interactive)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        using (var child = new Process { StartInfo = start })
        using (var progress = CreateProgressWindow())
        using (var timer = new Timer { Interval = 100 })
        {
            var output = new Queue<string>();
            bool ready = false;
            DataReceivedEventHandler receive = (sender, e) =>
            {
                if (string.IsNullOrWhiteSpace(e.Data))
                    return;
                lock (output)
                {
                    if (e.Data == Ready)
                    {
                        ready = true;
                        output.Clear();
                        return;
                    }
                    if (output.Count == 8)
                        output.Dequeue();
                    output.Enqueue(e.Data.Length > 300 ? e.Data.Substring(0, 300) + "…" : e.Data);
                }
            };
            child.OutputDataReceived += receive;
            child.ErrorDataReceived += receive;
            child.Start();
            child.BeginOutputReadLine();
            child.BeginErrorReadLine();

            var elapsed = Stopwatch.StartNew();
            long failedAt = -1;
            int exitCode = 1;
            timer.Tick += (sender, e) =>
            {
                if (child.HasExited)
                {
                    exitCode = child.ExitCode;
                    if (exitCode != 0)
                    {
                        // Allow the last redirected lines to arrive without waiting on inherited pipes.
                        if (failedAt < 0)
                            failedAt = elapsed.ElapsedMilliseconds;
                        if (elapsed.ElapsedMilliseconds - failedAt < 200)
                            return;
                    }
                    timer.Stop();
                    progress.Hide();
                    if (exitCode != 0)
                    {
                        string detail;
                        lock (output)
                            detail = string.Join(Environment.NewLine, output.ToArray());
                        ShowError(detail, interactive);
                    }
                    Application.ExitThread();
                    return;
                }

                bool isReady;
                lock (output)
                    isReady = ready;
                if (isReady)
                    progress.Hide();
                else if (interactive && !progress.Visible && elapsed.ElapsedMilliseconds >= 700)
                    progress.Show();
            };
            timer.Start();
            Application.Run();
            return exitCode;
        }
    }

    private static Form CreateProgressWindow()
    {
        var window = new Form
        {
            Text = Title,
            ClientSize = new Size(390, 104),
            StartPosition = FormStartPosition.CenterScreen,
            FormBorderStyle = FormBorderStyle.FixedDialog,
            ControlBox = false,
            AutoScaleMode = AutoScaleMode.Font
        };
        window.Controls.Add(new Label
        {
            Text = "正在启动。首次使用需要联网安装运行环境。",
            Location = new Point(18, 18),
            AutoSize = true
        });
        window.Controls.Add(new ProgressBar
        {
            Location = new Point(18, 52),
            Size = new Size(354, 17),
            Style = ProgressBarStyle.Marquee
        });
        return window;
    }

    private static void ShowError(string detail, bool interactive)
    {
        string message = "启动失败。可运行 run.bat 查看详细错误。";
        if (!string.IsNullOrWhiteSpace(detail))
            message += Environment.NewLine + Environment.NewLine + detail;
        if (!interactive)
        {
            using (var writer = new StreamWriter(Console.OpenStandardError(), new UTF8Encoding(false)))
                writer.WriteLine(message);
            return;
        }
        MessageBox.Show(message, Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    // Quote a single argument using the Windows argv backslash and quote rules.
    private static string Quote(string value)
    {
        var result = new StringBuilder().Append('"');
        int backslashes = 0;
        foreach (char character in value)
        {
            if (character == '\\')
            {
                backslashes++;
                continue;
            }
            if (character == '"')
                result.Append('\\', backslashes * 2 + 1);
            else
                result.Append('\\', backslashes);
            result.Append(character);
            backslashes = 0;
        }
        return result.Append('\\', backslashes * 2).Append('"').ToString();
    }
}
