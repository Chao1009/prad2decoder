#include <QApplication>
#include <QWebEngineView>
#include <QWebEnginePage>
#include <QCommandLineParser>
#include <QUrl>
#include <QMessageBox>

int main(int argc, char *argv[])
{
    QApplication app(argc, argv);
    app.setApplicationName("prad2_monitor");
    app.setApplicationVersion("1.0.0");

    QCommandLineParser parser;
    parser.setApplicationDescription("Lightweight Qt WebEngine client for PRad2 event monitor");
    parser.addHelpOption();
    parser.addVersionOption();
    parser.addPositionalArgument("url", "URL to load (default: http://localhost:5050)",
                                "[url]");

    parser.process(app);

    QString url = "http://localhost:5050";
    const QStringList args = parser.positionalArguments();
    if (!args.isEmpty()) {
        url = args.first();
    }

    QWebEngineView view;
    view.setWindowTitle("PRad2 Monitor - " + url);
    view.resize(1280, 800);

    // show connection error instead of blank white page
    QObject::connect(view.page(), &QWebEnginePage::loadFinished,
                     [&view, &url](bool ok) {
        if (!ok) {
            view.setHtml(QString(
                "<html><body style='font-family:sans-serif;padding:40px;color:#555;'>"
                "<h2>Could not connect</h2>"
                "<p>Failed to load <b>%1</b></p>"
                "<p>Make sure the server (evc_viewer or evc_monitor) is running.</p>"
                "</body></html>").arg(url));
        }
    });

    view.load(QUrl(url));
    view.show();

    return app.exec();
}
