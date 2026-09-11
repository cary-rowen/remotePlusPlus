# Remote PlusPlus

Gelişmiş kullanıcılar için NVDA Remote eklentisini verimlilik özellikleriyle güçlendirir.

## Özellikler

### Bağlantı Yöneticisi

Sık kullanılan uzak oturumları hızlıca kaydedin, düzenleyin ve bağlanın.

* Bağlantıları gruplar halinde düzenleme
* Bağlantıları ad veya sunucuya göre arama
* Kayıtlı listeden tek tıklamayla bağlanma
* Bağlantıyı panoya kopyalama

### Kontrol Modunu Değiştir

Bağlantıyı kesmeden başka bir bilgisayarı kontrol etme ile kontrol edilme arasında anında geçiş yapın.

### Varsayılan Sunucuya Bağlan

Yapılandırdığınız otomatik bağlantı sunucusuna tek bir kısayolla hızlıca bağlanın.

## Bağlantı Yöneticisi

Bağlantı Yöneticisi, uzak bağlantılarınızı yönetmek için kullanışlı bir arayüz sağlar.

### Liste Kısayolları

| İşlem | Kısayol |
|--------|----------|
| Ters modda bağlan | `Shift+Enter` |
| Bağlantıyı yukarı taşı | `Alt+Yukarı Ok` |
| Bağlantıyı aşağı taşı | `Alt+Aşağı Ok` |
| Bağlantıyı düzenle | `F2` |
| Bağlantıyı sil | `Delete` |
| Tümünü seç | `Ctrl+A` |
| Bağlantıyı kopyala | `Ctrl+C` |

### Bağlam Menüsü

Ek seçeneklere erişmek için bir bağlantıya sağ tıklayın:

* Bağlan / Ters Bağlan
* Düzenle
* Bağlantıyı kopyala
* Otomatik Bağlan Olarak Ayarla
* Yukarı Taşı / Aşağı Taşı
* Sil

## Klavye Kısayolları

| Komut | Kısayol |
|---------|---------|
| Bağlantı Yöneticisini Aç | `NVDA+Control+Shift+N` |
| Kontrol Modunu Değiştir | `NVDA+Control+Shift+W` |
| Varsayılan Sunucuya Bağlan | Atanmamış |

## Menü Öğeleri

Tüm özelliklere NVDA Remote menüsünden de erişilebilir:

* Bağlantı Yöneticisi...
* Kontrol Modunu Değiştir
* Varsayılan Sunucuya Bağlan (Yalnızca otomatik bağlantı yapılandırıldığında gösterilir)

## Gereksinimler

* NVDA 2026.1 veya daha yenisi (yerleşik Uzaktan Erişim etkinleştirilmiş olmalıdır)

## Güvenlik

Maksimum güvenliği sağlamak ve istenmeyen erişimi önlemek için, bu eklenti güvenli masaüstünde (örn. UAC istemleri, Windows oturum açma ekranı) devre dışı bırakılmıştır.

## Geliştirici

Cary-rowen <manchen_0528@outlook.com>

## Düşük gecikmeli ses

Düşük gecikmeli ses, denetlenen bilgisayarın sesini `NVDARemoteAudioServer`
üzerinden aktarır. Geçerli Remote bağlantısının ana bilgisayarı otomatik olarak
kullanılır, ses portu `6838` olarak sabittir ve Remote anahtarı yeniden kullanılır.
Bağlantı oluşturulduğunda ses kapalıdır; bağlantı düzenleyicisinde ses ayarı yoktur.

Remote Access menüsünde iki bağımsız seçenek bulunur:

* Uzak sistemin Windows varsayılan çıkış aygıtındaki seslerini dinle
* Uzak mikrofonu dinle

Seçenekler yalnızca denetleyen bilgisayarda kullanılabilir. Seçim, denetlenen
bilgisayardan ilgili kaynağı başlatmasını ister; iki kaynak aynı anda açılabilir
ve orada karıştırılır. Denetlenen bilgisayarda Remote++ ve pakete dahil ses
modülü bulunmalıdır. Eski bir Remote istemcisi, Remote++ veya ses modülü yoksa
ses kullanılamaz ya da istek zaman aşımına uğrar; Remote bağlantısı normal kalır.
Varsayılan olarak ses kısayolu atanmaz.

NVDA Ayarları → Remote++ bölümünde oynatma arabelleği (en az, varsayılan;
10, 20, 40 veya 80 ms) ve aktarım kalitesi (48 kHz stereo, varsayılan;
48, 24 veya 16 kHz mono) seçilebilir. Genel tercihler tüm bağlantılara uygulanır.
Uygula, seçili kaynakları koruyarak etkin dinlemeyi kısa süreli yeniden başlatır;
kapalı sesi açmaz. Eski ses istemcilerinde 48 kHz stereo kullanılır ve tercih korunur.

Python modülü 16 bit PCM ve 5 ms çerçeveler kullanır; varsayılan 48 kHz stereodur. Ses
protokolü şifrelenmez; güvenilir ağ veya VPN kullanın.
