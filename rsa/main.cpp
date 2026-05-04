#include <iostream>
#include <fstream>
#include <vector>
#include <cstring>
#include <cstdlib>
#include <cmath>

using namespace std;

// проверка, является ли число простым
bool isPrime(long long n) {
    if (n < 2) return false;
    if (n == 2) return true;
    if (n % 2 == 0) return false;
    for (long long i = 3; i * i <= n; i += 2) {
        if (n % i == 0) return false;
    }
    return true;
}

// наибольший общий делитель
long long gcd(long long a, long long b) {
    while (b != 0) {
        long long tmp = b;
        b = a % b;
        a = tmp;
    }
    return a;
}

// быстрое возведение в степень по модулю: a^z mod n
long long fastExp(long long a, long long z, long long n) {
    long long a1 = a;
    long long z1 = z;
    long long x = 1;
    a1 = a1 % n;
    while (z1 > 0) {
        while (z1 % 2 == 0) {
            z1 = z1 / 2;
            a1 = (a1 * a1) % n;
        }
        z1 = z1 - 1;
        x = (x * a1) % n;
    }
    return x;
}

// расширенный алгоритм Евклида
// находит x1, y1 такие что x1*a + y1*b = gcd(a,b)
// если a и b взаимно просты, то y1 — мультипликативное инверсное b по модулю a
long long euclidEx(long long a, long long b) {
    long long d0 = a, d1 = b;
    long long x0 = 1, x1 = 0;
    long long y0 = 0, y1 = 1;

    while (d1 > 1) {
        long long q = d0 / d1;
        long long d2 = d0 % d1;
        long long x2 = x0 - q * x1;
        long long y2 = y0 - q * y1;
        d0 = d1; d1 = d2;
        x0 = x1; x1 = x2;
        y0 = y1; y1 = y2;
    }

    if (y1 < 0)
        y1 += a;

    return y1;
}

// факторизация числа r (поиск множителей p и q)
bool factorize(long long r, long long &p, long long &q) {
    for (long long i = 2; i * i <= r; i++) {
        if (r % i == 0) {
            p = i;
            q = r / i;
            if (isPrime(p) && isPrime(q))
                return true;
        }
    }
    return false;
}

// ========== 1. ШИФРОВАНИЕ ==========
void encryptFile() {
    long long p, q, Kc;
    char inputFile[256], outputFile[256];

    cout << "=== Шифрование RSA ===" << endl;
    cout << "Введите p (простое число): ";
    cin >> p;
    cout << "Введите q (простое число): ";
    cin >> q;
    cout << "Введите закрытый ключ Kc: ";
    cin >> Kc;
    cout << "Введите имя входного файла: ";
    cin >> inputFile;
    cout << "Введите имя выходного файла: ";
    cin >> outputFile;

    // проверки
    if (!isPrime(p)) {
        cerr << "Ошибка: p = " << p << " не является простым числом!" << endl;
        return;
    }
    if (!isPrime(q)) {
        cerr << "Ошибка: q = " << q << " не является простым числом!" << endl;
        return;
    }
    if (p == q) {
        cerr << "Ошибка: p и q должны быть различными!" << endl;
        return;
    }

    long long r = p * q;
    long long phi = (p - 1) * (q - 1);

    if (r < 256) {
        cerr << "Ошибка: r = p*q = " << r << " слишком мало (должно быть >= 256 для побайтового шифрования)!" << endl;
        return;
    }

    if (Kc <= 1 || Kc >= phi) {
        cerr << "Ошибка: Kc должен быть в диапазоне (1, " << phi << ")!" << endl;
        return;
    }
    if (gcd(Kc, phi) != 1) {
        cerr << "Ошибка: Kc = " << Kc << " и phi(r) = " << phi << " не взаимно простые!" << endl;
        return;
    }

    // вычисление открытого ключа Ko
    long long Ko = euclidEx(phi, Kc);

    cout << endl;
    cout << "Параметры RSA:" << endl;
    cout << "  p = " << p << endl;
    cout << "  q = " << q << endl;
    cout << "  r = p*q = " << r << endl;
    cout << "  phi(r) = " << phi << endl;
    cout << "  Kc (закрытый ключ) = " << Kc << endl;
    cout << "  Ko (открытый ключ) = " << Ko << endl;

    // проверка правильности вычисления Ko
    if ((Ko * Kc) % phi != 1) {
        cerr << "Ошибка вычисления открытого ключа!" << endl;
        return;
    }

    // чтение входного файла
    ifstream fin(inputFile, ios::binary);
    if (!fin) {
        cerr << "Ошибка: не удалось открыть файл " << inputFile << endl;
        return;
    }

    ofstream fout(outputFile, ios::binary);
    if (!fout) {
        cerr << "Ошибка: не удалось открыть файл " << outputFile << endl;
        return;
    }

    // побайтовое шифрование: каждый байт -> 16-битное значение (2 байта, big-endian)
    unsigned char byte;
    while (fin.read((char*)&byte, 1)) {
        long long encrypted = fastExp(byte, Ko, r);
        unsigned char high = (encrypted >> 8) & 0xFF;
        unsigned char low = encrypted & 0xFF;
        fout.write((char*)&high, 1);
        fout.write((char*)&low, 1);
    }

    fin.close();
    fout.close();

    cout << "Шифрование завершено. Результат записан в " << outputFile << endl;
}

// ========== 2. РАСШИФРОВАНИЕ ==========
void decryptFile() {
    long long r, Kc;
    char inputFile[256], outputFile[256];

    cout << "=== Расшифрование RSA ===" << endl;
    cout << "Введите модуль r: ";
    cin >> r;
    cout << "Введите закрытый ключ Kc: ";
    cin >> Kc;
    cout << "Введите имя входного файла: ";
    cin >> inputFile;
    cout << "Введите имя выходного файла: ";
    cin >> outputFile;

    if (r < 256) {
        cerr << "Ошибка: r = " << r << " слишком мало!" << endl;
        return;
    }
    if (Kc <= 0) {
        cerr << "Ошибка: Kc должен быть положительным!" << endl;
        return;
    }

    ifstream fin(inputFile, ios::binary);
    if (!fin) {
        cerr << "Ошибка: не удалось открыть файл " << inputFile << endl;
        return;
    }

    ofstream fout(outputFile, ios::binary);
    if (!fout) {
        cerr << "Ошибка: не удалось открыть файл " << outputFile << endl;
        return;
    }

    // каждый 16-битный блок (2 байта) -> расшифровываем в 1 байт
    unsigned char high, low;
    while (fin.read((char*)&high, 1) && fin.read((char*)&low, 1)) {
        long long encrypted = (high << 8) | low;
        long long decrypted = fastExp(encrypted, Kc, r);
        unsigned char byte = decrypted & 0xFF;
        fout.write((char*)&byte, 1);
    }

    fin.close();
    fout.close();

    cout << "Расшифрование завершено. Результат записан в " << outputFile << endl;
}

// ========== 3. ВЗЛОМ (ДЕШИФРОВАНИЕ) ==========
void crackFile() {
    long long r, Ko;
    char inputFile[256], outputFile[256];

    cout << "=== Взлом RSA ===" << endl;
    cout << "Введите модуль r: ";
    cin >> r;
    cout << "Введите открытый ключ Ko: ";
    cin >> Ko;
    cout << "Введите имя входного файла: ";
    cin >> inputFile;
    cout << "Введите имя выходного файла: ";
    cin >> outputFile;

    if (r < 256) {
        cerr << "Ошибка: r = " << r << " слишком мало!" << endl;
        return;
    }

    // факторизация r для нахождения p и q
    long long p, q;
    if (!factorize(r, p, q)) {
        cerr << "Ошибка: не удалось разложить r = " << r << " на простые множители!" << endl;
        return;
    }

    long long phi = (p - 1) * (q - 1);

    if (gcd(Ko, phi) != 1) {
        cerr << "Ошибка: Ko и phi(r) не взаимно простые!" << endl;
        return;
    }

    // вычисление закрытого ключа из открытого
    long long Kc = euclidEx(phi, Ko);

    cout << endl;
    cout << "Взлом:" << endl;
    cout << "  r = " << r << endl;
    cout << "  Найдены множители: p = " << p << ", q = " << q << endl;
    cout << "  phi(r) = " << phi << endl;
    cout << "  Ko (открытый ключ) = " << Ko << endl;
    cout << "  Kc (вычисленный закрытый ключ) = " << Kc << endl;

    ifstream fin(inputFile, ios::binary);
    if (!fin) {
        cerr << "Ошибка: не удалось открыть файл " << inputFile << endl;
        return;
    }

    ofstream fout(outputFile, ios::binary);
    if (!fout) {
        cerr << "Ошибка: не удалось открыть файл " << outputFile << endl;
        return;
    }

    // дешифрование аналогично расшифрованию, но с вычисленным Kc
    unsigned char high, low;
    while (fin.read((char*)&high, 1) && fin.read((char*)&low, 1)) {
        long long encrypted = (high << 8) | low;
        long long decrypted = fastExp(encrypted, Kc, r);
        unsigned char byte = decrypted & 0xFF;
        fout.write((char*)&byte, 1);
    }

    fin.close();
    fout.close();

    cout << "Дешифрование (взлом) завершено. Результат записан в " << outputFile << endl;
}

int main(int argc, char* argv[]) {
    if (argc != 2) {
        cout << "Использование:" << endl;
        cout << "  " << argv[0] << " encrypt   — шифрование (ввод p, q, Kc)" << endl;
        cout << "  " << argv[0] << " decrypt   — расшифрование (ввод r, Kc)" << endl;
        cout << "  " << argv[0] << " crack     — взлом/дешифрование (ввод r, Ko)" << endl;
        return 1;
    }

    string mode = argv[1];

    if (mode == "encrypt") {
        encryptFile();
    } else if (mode == "decrypt") {
        decryptFile();
    } else if (mode == "crack") {
        crackFile();
    } else {
        cerr << "Ошибка: неизвестный режим '" << mode << "'." << endl;
        return 1;
    }

    return 0;
}
